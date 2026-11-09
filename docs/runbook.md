# Runbook: deploy once to real AWS, destroy, stay at $0

Do this only after everything passes end to end on `kind`. Sign up at that point, so the Free plan's 6-month window covers the deploy and the load test.

Scripts handle steps 6, 7, 11 and 13. The other steps stay manual. Every resource gets the `Project=telemetry-monitor` tag through the provider's `default_tags`.

## Before (manual, once)

1. [ ] Create a **new AWS account** on the Free plan. Never upgrade it.
2. [ ] Turn on MFA for the root login. Create the admin IAM user with MFA, then lock the root login away.
3. [ ] Run `terraform/bootstrap` apply to create the $0.01 and $1 budgets.

## One sitting (a few hours, never left running)

4. [ ] Run `terraform/main` apply. *Record it.*
5. [ ] Confirm the SNS email subscriptions.
6. [ ] Run `scripts/aws-key.sh`. It creates the demo access key and writes it into the Kubernetes Secret `aws-credentials`.
7. [ ] Run `scripts/aws-up.sh`. It writes `k8s/overlays/aws/aws.env` from the Terraform outputs and runs `kubectl apply -k k8s/overlays/aws`.
8. [ ] Run the demo: inject a fault, see the alert through `GET /alerts`, and receive the email. *Record it and turn it into a GIF.*
9. [ ] Run the load test and show KEDA scaling. *Record it.* The numbers belong to #13.
10. [ ] Take console screenshots of the queue, the tables, the alarm, and the billing page at $0.
11. [ ] Run `scripts/aws-down.sh` (`kubectl delete -k k8s/overlays/aws`), then `scripts/aws-key.sh --delete` to delete the Secret and the IAM access key.
12. [ ] Run `terraform/main` destroy. *Record it.*
13. [ ] Run `scripts/aws-cleanup-check.sh`. It prints the account it checked and fails if any resource with the project tag is left in us-west-2 (the bootstrap budgets aside, kept until step 15), or if the demo IAM user or any of its access keys is left. The admin user's keys are yours to manage. Then confirm the billing page shows $0.

## After

14. [ ] Once the cleanup check passes, the local `terraform/main` state file can be deleted.
15. [ ] After the demo and the AWS numbers are captured, **close the account by hand**. The bootstrap budgets go with it.

## Notes

- Steps 6 to 7 need the `kind` cluster, images and Envoy Gateway from `scripts/kind-up.sh`. `scripts/aws-up.sh` first removes the emulators and the init Job of the `local` overlay. It keeps the `telemetry` Namespace and the `aws-credentials` Secret from step 6.
- `scripts/aws-key.sh` refuses to create a key while the demo user already has one: run `scripts/aws-key.sh --delete` first.
- Run step 11 before step 12: the scripts read the demo user name and the Terraform outputs from the state.
- `aws.env` is generated and gitignored. Nothing account-specific is committed. To render the overlay offline, copy the sample first: `cp k8s/overlays/aws/aws.env.example k8s/overlays/aws/aws.env`, then `kubectl kustomize k8s/overlays/aws`.
- The budgets only alert. There is no automatic kill switch, because the Free plan can't bill. They catch mistakes such as an accidental upgrade, and the response is to run destroy.
- Never leave the stack up across sessions: it is easy to forget it is running.
