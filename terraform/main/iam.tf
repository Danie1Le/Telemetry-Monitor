# The access key is created by a CLI script so the secret never enters state.
resource "aws_iam_user" "demo" {
  name = "telemetry-monitor-demo"
}

data "aws_iam_policy_document" "demo" {
  statement {
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.readings.arn, aws_sqs_queue.dlq.arn]
  }

  statement {
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.readings.arn, aws_dynamodb_table.alerts.arn]
  }

  statement {
    actions   = ["sns:Publish"]
    resources = [for t in aws_sns_topic.topic : t.arn]
  }
}

resource "aws_iam_user_policy" "demo" {
  name   = "telemetry-monitor-demo"
  user   = aws_iam_user.demo.name
  policy = data.aws_iam_policy_document.demo.json
}
