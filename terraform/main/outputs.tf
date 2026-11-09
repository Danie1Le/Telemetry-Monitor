output "readings_queue_url" {
  value = aws_sqs_queue.readings.url
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "readings_table" {
  value = aws_dynamodb_table.readings.name
}

output "alerts_table" {
  value = aws_dynamodb_table.alerts.name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.topic["alerts"].arn
}

output "ops_topic_arn" {
  value = aws_sns_topic.topic["ops"].arn
}

output "demo_user_name" {
  value = aws_iam_user.demo.name
}
