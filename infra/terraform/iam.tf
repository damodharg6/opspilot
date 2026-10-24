data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  log_group_arn = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/${var.project}/*"
}

# --- EC2 instance role: no access keys on the server -----------------------------------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.project}-app"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "WriteOwnContainerLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = [local.log_group_arn, "${local.log_group_arn}:*"]
  }

  statement {
    sid       = "QueryOwnLogs"
    actions   = ["logs:StartQuery", "logs:StopQuery"]
    resources = [local.log_group_arn]
  }

  statement {
    # GetQueryResults and GetMetricData do not support resource-level permissions.
    sid       = "ReadQueryResultsAndMetrics"
    actions   = ["logs:GetQueryResults", "cloudwatch:GetMetricData"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.project}/*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "${var.project}-app-readonly"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  # Session Manager shell access: no open SSH port needed.
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.project}-app"
  role = aws_iam_role.app.name
}

# --- Lambda role -------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "forwarder" {
  name               = "${var.project}-alarm-forwarder"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "forwarder_vpc" {
  role       = aws_iam_role.forwarder.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}
