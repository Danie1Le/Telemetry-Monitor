# The access key is created by a CLI script so the secret never enters state.
resource "aws_iam_user" "demo" {
  name = "telemetry-monitor-demo"
}

data "aws_iam_policy_document" "demo" {
  # Ingest sends, the processor receives and deletes, KEDA and scripts/measure.py (runbook step 9) read the depth.
  statement {
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.readings.arn]
  }

  # The processor only sends to the DLQ; GetQueueAttributes is for scripts/measure.py (runbook step 9).
  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.dlq.arn]
  }

  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.readings.arn]
  }

  # The processor writes alerts, ingest queries them.
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.alerts.arn]
  }

  # Only the alerts topic; CloudWatch alone publishes to ops.
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.topic["alerts"].arn]
  }
}

resource "aws_iam_user_policy" "demo" {
  name   = "telemetry-monitor-demo"
  user   = aws_iam_user.demo.name
  policy = data.aws_iam_policy_document.demo.json
}
