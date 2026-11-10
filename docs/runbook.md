# Runbook: deploy once to real AWS, destroy, stay at $0

Do this only after everything passes end to end on `kind`. Sign up at that point, so the Free plan's 6-month window covers the deploy and the load test.

Scripts handle steps 6, 7, 11 and 13. The other steps stay manual. Every resource gets the `Project=telemetry-monitor` tag through the provider's `default_tags`.

## Before (manual, once)

1. [ ] Create a **new AWS account** on the Free plan. Never upgrade it.
2. [ ] Turn on MFA for the root login. Create the admin IAM user with MFA, then lock the root login away.
3. [ ] Create the $0.01 and $1 budgets: `cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars`, set `alert_email` in it, then `terraform -chdir=terraform/bootstrap init` and `terraform -chdir=terraform/bootstrap apply`.

## One sitting (a few hours, never left running)

4. [ ] Create the queues, tables, topics and demo IAM user: `cp terraform/main/terraform.tfvars.example terraform/main/terraform.tfvars`, set `alert_email` in it, then `terraform -chdir=terraform/main init` and `terraform -chdir=terraform/main apply`. Optional: save the output.
   - [ ] **4a.** Right after, back up the state outside the repo: `cp terraform/main/terraform.tfstate ~/telemetry-main.tfstate.bak`. Keep it until the cleanup check in step 13 passes (step 14 deletes it). A lost state file leaves resources that `terraform destroy` cannot see and that you would delete by hand.
5. [ ] Confirm the SNS email subscriptions.
6. [ ] Run `scripts/aws-key.sh`. It creates the demo access key and writes it into the Kubernetes Secret `aws-credentials`.
7. [ ] Run `scripts/aws-up.sh`. It writes `k8s/overlays/aws/aws.env` from the Terraform outputs and runs `kubectl apply -k k8s/overlays/aws`.
8. [ ] Run the demo: inject a Fault, see the Alert through `GET /alerts`, and receive the email. Optional: record it, for example as a GIF. The simulator takes one Fault on `POST /faults` (port 8081, inside the cluster). A Flight lasts 180 s, and the Rules need the ascent Phase (40 to 160 s after the Flight starts, Rule graces over by 55 s), so post between 60 and 150 s after the Flight starts. A Fault posted in the 10 s idle gap is lost.
   ```bash
   kubectl --context kind-telemetry -n telemetry port-forward svc/simulator 8081:8081 &
   pf=$!
   kubectl --context kind-telemetry -n telemetry logs deploy/simulator | grep 'flight .* started' | tail -1
   flight=$(kubectl --context kind-telemetry -n telemetry logs deploy/simulator | grep 'flight .* started' | tail -1 | awk '{print $4}')
   # the log line starts with the Flight start time; wait until 60 s into that Flight, then:
   curl -i -X POST localhost:8081/faults -H 'Content-Type: application/json' -d '{"kind":"spike","engine":1,"sensor":"chamber_temp","durationSeconds":2}'   # 202
   curl "http://localhost:8080/alerts?flightId=$flight"   # threshold and rate_of_change on engine-1.chamber_temp, within ~30 s
   kill $pf
   ```
   The alert email goes to the SNS subscription from step 5. A 400 reply names the invalid field.
9. [ ] Run the load test and show KEDA scaling. Optional: save the output. The numbers go in [RESULTS.md](../RESULTS.md). The `readings` table has 20 WCU and one Batch writes 5 items (one per Sensor, 1 WCU each), so the table takes at most 20 / 5 = 4 Batches/s before it throttles. Run at **3 Batches/s** (15 WCU), and scale the simulator to 0 so it adds no writes (its engine writes another 5 WCU/s). `scripts/measure.py` works on AWS unchanged: the cluster is still `kind`, only the queue, tables and topic are real. Run it from the repo root:
   ```bash
   start=$(date -u +%FT%TZ)
   kubectl --context kind-telemetry -n telemetry scale deploy/simulator --replicas=0
   uv run --project simulator scripts/measure.py sustained --rate 3 --duration 600
   kubectl --context kind-telemetry -n telemetry scale deploy/simulator --replicas=1   # detection restarts the simulator
   uv run --project simulator scripts/measure.py detection --runs 3
   end=$(date -u +%FT%TZ)
   for t in readings alerts; do for m in WriteThrottleEvents ReadThrottleEvents; do
     echo "$t $m: $(aws cloudwatch get-metric-statistics --region us-west-2 --namespace AWS/DynamoDB --metric-name $m --dimensions Name=TableName,Value=$t --start-time $start --end-time $end --period 60 --statistics Sum --query 'sum(Datapoints[].Sum)' --output text)"
   done; done   # all four must be 0
   ```
   The run writes `results/<UTC date>/`. `measure.py` does not check throttling, so the loop above is the check. CloudWatch lags a few minutes: re-run the loop with a later `end` before trusting a 0. At 3 Batches/s the queue stays far below the KEDA target of 100 messages per replica, so expect one processor replica and no scale-up (`kubectl --context kind-telemetry -n telemetry get hpa -w`). Label the AWS numbers as AWS and the capped rate.
10. [ ] Optionally, take console screenshots of the queue, the tables, the alarm, and the billing page at $0.
11. [ ] Run `scripts/aws-down.sh` (`kubectl delete -k k8s/overlays/aws`), then `scripts/aws-key.sh --delete` to delete the Secret and the IAM access key.
12. [ ] Run `terraform/main` destroy. Optional: save the output.
13. [ ] Run `scripts/aws-cleanup-check.sh`. It prints the account it checked and fails if any resource with the project tag is left in us-west-2 (the bootstrap budgets aside, kept until step 15), or if the demo IAM user or any of its access keys is left. It checks only the demo user's keys, because the check itself runs with the admin key. The admin user's keys are yours to manage. Then confirm the billing page shows $0.

## After

14. [ ] Once the cleanup check passes, the local `terraform/main` state file and the backup from step 4a can be deleted.
15. [ ] After the demo and the AWS numbers are captured, **close the account by hand**. The bootstrap budgets go with it.

## Notes

- Steps 6 to 7 need the `kind` cluster, images, Envoy Gateway and KEDA from `scripts/kind-up.sh`. A cluster created before KEDA was added must re-run `scripts/kind-up.sh` before `scripts/aws-up.sh`. `scripts/aws-up.sh` first removes the emulators and the init Job of the `local` overlay. It keeps the `telemetry` Namespace and the `aws-credentials` Secret from step 6.
- `scripts/aws-key.sh` refuses to create a key while the demo user already has one: run `scripts/aws-key.sh --delete` first.
- Run step 11 before step 12: the scripts read the demo user name and the Terraform outputs from the state.
- `aws.env` is generated and gitignored. Nothing account-specific is committed. To render the overlay offline, copy the sample first: `cp k8s/overlays/aws/aws.env.example k8s/overlays/aws/aws.env`, then `kubectl kustomize k8s/overlays/aws`.
- The budgets only alert. There is no automatic kill switch, because the Free plan can't bill. They catch mistakes such as an accidental upgrade, and the response is to run destroy.
- Never leave the stack up across sessions: it is easy to forget it is running.
