resource "aws_cloudwatch_log_group" "shop" {
  name              = "/${var.project}/shop"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.project}/app"
  retention_in_days = 7
}

locals {
  namespace = "OpsPilot/Shop"

  # Names must match METRIC_QUERIES in mcp_servers/backends/aws.py
  metric_filters = {
    RequestCount = { pattern = "{ $.event = \"http_request\" }", value = "1" }
    Errors5xx    = { pattern = "{ $.event = \"http_request\" && $.status >= 500 }", value = "1" }
    LatencyMs    = { pattern = "{ $.event = \"http_request\" }", value = "$.latency_ms" }
    MemoryMb     = { pattern = "{ $.event = \"runtime_stats\" }", value = "$.rss_mb" }
  }
}

resource "aws_cloudwatch_log_metric_filter" "shop" {
  for_each       = local.metric_filters
  name           = "${var.project}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.shop.name
  pattern        = each.value.pattern

  metric_transformation {
    name      = each.key
    namespace = local.namespace
    value     = each.value.value
    unit      = "None"
  }
}

# --- alarms: each one starts an investigation ---------------------------------------------
resource "aws_cloudwatch_metric_alarm" "error_rate" {
  alarm_name          = "${var.project}-shop-5xx-rate-high"
  alarm_description   = "Shop 5xx error rate above ${var.error_rate_threshold}%"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.error_rate_threshold
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "rate"
    expression  = "100 * FILL(e, 0) / r"
    label       = "5xx rate %"
    return_data = true
  }
  metric_query {
    id = "e"
    metric {
      namespace   = local.namespace
      metric_name = "Errors5xx"
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "r"
    metric {
      namespace   = local.namespace
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"
    }
  }

  depends_on = [aws_cloudwatch_log_metric_filter.shop]
}

resource "aws_cloudwatch_metric_alarm" "latency" {
  alarm_name          = "${var.project}-shop-latency-p95-high"
  alarm_description   = "Shop p95 latency above 1500 ms"
  namespace           = local.namespace
  metric_name         = "LatencyMs"
  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1500
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.shop]
}

resource "aws_cloudwatch_metric_alarm" "memory" {
  alarm_name          = "${var.project}-shop-memory-high"
  alarm_description   = "Shop resident memory above 400 MB"
  namespace           = local.namespace
  metric_name         = "MemoryMb"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 400
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.shop]
}
