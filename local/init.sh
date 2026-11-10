#!/bin/sh
# Creates the local DynamoDB tables and SNS topics. Idempotent. Reused by the k8s `local` overlay.
set -eu
: "${DYNAMODB_ENDPOINT:?}" "${SNS_ENDPOINT:?}"
ddb="aws --endpoint-url $DYNAMODB_ENDPOINT dynamodb"
sns="aws --endpoint-url $SNS_ENDPOINT sns"

for i in $(seq 1 60); do
  $ddb list-tables >/dev/null 2>&1 && $sns list-topics >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "emulators not ready" >&2; exit 1; }
  sleep 1
done

# create_table <name> <hash> <hash type> <range> <range type> <rcu> <wcu>
create_table() {
  if $ddb describe-table --table-name "$1" >/dev/null 2>&1; then echo "table $1 exists"; return; fi
  $ddb create-table --table-name "$1" \
    --attribute-definitions "AttributeName=$2,AttributeType=$3" "AttributeName=$4,AttributeType=$5" \
    --key-schema "AttributeName=$2,KeyType=HASH" "AttributeName=$4,KeyType=RANGE" \
    --provisioned-throughput "ReadCapacityUnits=$6,WriteCapacityUnits=$7" >/dev/null
  $ddb update-time-to-live --table-name "$1" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" >/dev/null
  echo "table $1 created"
}

create_table readings sensorId S second N 20 20
create_table alerts flightId S alertKey S 5 5
$sns create-topic --name alerts >/dev/null; echo "topic alerts ok"
