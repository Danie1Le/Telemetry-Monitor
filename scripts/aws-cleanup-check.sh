#!/usr/bin/env bash
# Run after `terraform destroy`. Exits non-zero, listing what is left, if any resource tagged
# Project=telemetry-monitor remains in us-east-2 (the kept bootstrap budgets aside), or the demo IAM user
# or any of its access keys exists.
# Usage: scripts/aws-cleanup-check.sh
set -euo pipefail

case "${1:-}" in
  -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac
command -v aws >/dev/null || { echo "aws not found" >&2; exit 1; }

# Fixed name from terraform/main/iam.tf; Terraform outputs are gone after destroy.
user=telemetry-monitor-demo
left=0
# Also fails fast on bad credentials, and shows which account was checked.
# tr: the Windows aws CLI ends text output with CRLF, and a stray \r would corrupt messages and make an empty list look non-empty.
account=$(aws sts get-caller-identity --query Account --output text | tr -d '\r')

# The bootstrap budgets carry the same tag and are kept on purpose until the account is closed.
arns=$(aws resourcegroupstaggingapi get-resources --region us-east-2 \
  --tag-filters Key=Project,Values=telemetry-monitor \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr -d '\r')
arns=$(printf '%s' "$arns" | tr '\t' '\n' | grep -v ':budgets::' || true)
if [ -n "$arns" ]; then echo "tagged resources left in account $account:"; echo "$arns"; left=1; fi

# IAM is global and the tagging API can miss users, so ask IAM directly. Only NoSuchEntity means gone.
if err=$(aws iam get-user --user-name "$user" 2>&1 >/dev/null); then
  echo "IAM user left in account $account: $user"
  keys=$(aws iam list-access-keys --user-name "$user" --query 'AccessKeyMetadata[].AccessKeyId' --output text | tr -d '\r')
  if [ -n "$keys" ]; then echo "access keys left: $keys"; fi
  left=1
elif [[ $err != *NoSuchEntity* ]]; then
  echo "cannot check IAM user: $err" >&2
  exit 1
fi

if [ "$left" = 0 ]; then echo "clean: account $account has nothing tagged Project=telemetry-monitor in us-east-2 (budgets aside), no demo user or access keys"; fi
exit "$left"
