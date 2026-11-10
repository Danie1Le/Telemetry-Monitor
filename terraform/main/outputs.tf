output "readings_queue_url" {
  value = aws_sqs_queue.readings.url
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "alerts_topic_arn" {
  value = aws_sns_topic.topic["alerts"].arn
}

output "demo_user_name" {
  value = aws_iam_user.demo.name
}
