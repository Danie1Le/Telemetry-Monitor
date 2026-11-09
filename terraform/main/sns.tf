resource "aws_sns_topic" "topic" {
  for_each = toset(["alerts", "ops"])

  name = each.key
}

resource "aws_sns_topic_subscription" "email" {
  for_each = aws_sns_topic.topic

  topic_arn = each.value.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
