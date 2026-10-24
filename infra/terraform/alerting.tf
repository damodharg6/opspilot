resource "aws_sns_topic" "alarms" {
  name = "${var.project}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "archive_file" "forwarder" {
  type        = "zip"
  source_file = "${path.module}/../lambda/alarm_forwarder.py"
  output_path = "${path.module}/../lambda/alarm_forwarder.zip"
}

resource "aws_lambda_function" "forwarder" {
  function_name    = "${var.project}-alarm-forwarder"
  role             = aws_iam_role.forwarder.arn
  runtime          = "python3.12"
  handler          = "alarm_forwarder.handler"
  filename         = data.archive_file.forwarder.output_path
  source_code_hash = data.archive_file.forwarder.output_base64sha256
  timeout          = 15
  memory_size      = 128

  vpc_config {
    subnet_ids         = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      OPSPILOT_URL        = "http://${aws_instance.app.private_ip}:8080"
      OPSPILOT_API_SECRET = var.opspilot_api_secret # encrypted at rest; see infra/README.md
      SERVICE_NAME        = "shop"
    }
  }
}

resource "aws_cloudwatch_log_group" "forwarder" {
  name              = "/aws/lambda/${aws_lambda_function.forwarder.function_name}"
  retention_in_days = 7
}

resource "aws_lambda_permission" "sns" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.forwarder.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alarms.arn
}

resource "aws_sns_topic_subscription" "forwarder" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.forwarder.arn
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
