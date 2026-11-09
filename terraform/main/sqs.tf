resource "aws_sqs_queue" "dlq" {
  name                      = "telemetry-readings-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "readings" {
  name                       = "telemetry-readings"
  visibility_timeout_seconds = 30

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}
