# Provisioned with no auto scaling: 25 RCU/WCU is the always-free ceiling across both tables.
resource "aws_dynamodb_table" "readings" {
  name           = "readings"
  billing_mode   = "PROVISIONED"
  read_capacity  = 20
  write_capacity = 20
  hash_key       = "sensorId"
  range_key      = "second"

  attribute {
    name = "sensorId"
    type = "S"
  }

  attribute {
    name = "second"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "alerts" {
  name           = "alerts"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "flightId"
  range_key      = "alertKey"

  attribute {
    name = "flightId"
    type = "S"
  }

  attribute {
    name = "alertKey"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
