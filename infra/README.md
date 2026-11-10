# infra

chase's cloud side: an evidence bucket, the `report` Cloud Run service, a GCS→Pub/Sub trigger,
keyless CI, and alerts. Everything is Terraform in `envs/prod/` except the one-time steps that
`bootstrap.sh` walks you through. Agents never run `terraform apply`; you do.

```
OptiPlex listener ──(create-only key)──▶ gs://<project>-chase-evidence
                                           episodes/<id>/{audio.flac,metadata.json}   30 days
                                           violations/<vid>/rev-<n>.json  ──▶ Pub/Sub ──▶ report (Cloud Run, scales to 0)
                                           evidence/<vid>/...  ◀── copies, MP3s, report.pdf, sent.json   365 days
                                                                       │
                                                                       └──▶ Gmail SMTP ──▶ your inbox (ready to forward)
```

| File | What |
|---|---|
| `storage.tf` | evidence bucket (private, lifecycle) and the Artifact Registry repo |
| `listener.tf` | `chase-listener`: create-only on `episodes/` + `violations/` ([ADR 0004](../docs/adr/0004-optiplex-service-account-key.md)) |
| `report.tf` | `chase-report` service account, the two secrets, and the Cloud Run service |
| `pubsub.tf` | GCS notification → topic → push subscription (600 s ack, dead letter) ([ADR 0005](../docs/adr/0005-report-trigger-and-idempotency.md)) |
| `wif.tf` | GitHub Actions → Google via Workload Identity Federation; `chase-ci` deploys from main only |
| `monitoring.tf` | email alerts: report failures, dead letters |

**Cost:** nothing at idle. Cloud Run scales to zero, Pub/Sub, logging and alerting stay inside
the free tier at this volume, and the bucket holds a few hundred MB.

## Morning checklist (first-time setup)

1. **Bootstrap and apply.** Run `bash infra/bootstrap.sh`. It creates the project, links
   billing, creates the state bucket, writes `terraform.tfvars`, and runs `terraform init`. When it
   says so, run `terraform plan` and `terraform apply` yourself in `infra/envs/prod`.
2. **Secrets.** Continue the script. It stores:
   - the Gmail app password (the Google account must have **2-Step Verification on**);
   - your report details: name, address, phone, sender and recipient emails, and neighboring
     addresses. These never touch the repo.
3. **Listener key.** The script creates `secrets/listener-key.json`. Copy it to the OptiPlex
   (`~/chase/secrets/listener-key.json`), add `gcs_bucket: <bucket>` to `config/chase.yaml` there,
   and run `make restart`.
4. **IAM smoke test.** Run `bash infra/smoke-iam.sh <bucket> secrets/listener-key.json`. Every line
   should say PASS.
5. **Deploy the real image.** Run the four `gh variable set …` lines the script prints, then
   re-run the *Report (build and deploy)* workflow (or push to main).
6. **One TEST report end to end.** Run `bash infra/send-test-violation.sh <bucket>`. Within a couple
   of minutes you should get an email whose subject starts with **`[TEST] Barking dog complaint`**,
   with the PDF and MP3s attached.
   - If nothing arrives, check the logs:
     `gcloud logging read 'resource.labels.service_name="report"' --freshness 15m`.
   - **If the logs show Pub/Sub push being rejected (403 or 404 before the app logs anything):**
     set `report_ingress = "INGRESS_TRAFFIC_ALL"` in `terraform.tfvars` and apply. IAM still lets
     only `chase-push` invoke the service.
7. **Confirm uploads from the OptiPlex.** Run `make status` there. `chase_outbox_pending` should
   drop to 0 after the next episode.

## Operations

- **Re-send a report.** Upload a copy of the manifest as the next revision
  (`violations/<vid>/rev-<n+1>.json`). Only the highest revision is reported, and its subject says
  "(updated)".
- **Dead letters** (you'll get an alert email):
  1. Inspect them with
     `gcloud pubsub subscriptions pull chase-violations-dead --limit 5 --auto-ack=false`.
  2. Fix the cause, which is usually in the report logs.
  3. Re-send the report as above.
- **Rotate the listener key:** see ADR 0004.
- **Attachment size:** reports attach MP3s while the email stays under `attachment_limit_mb`
  (default **15 MB, base64-encoded**). Over that, the audio is linked (links last 7 days). 311 or
  council mail servers may accept less than Gmail does. If a forward bounces, lower it in
  `terraform.tfvars` and apply.
