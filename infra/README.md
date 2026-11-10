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

The order matters: the OptiPlex is connected **last**, so nothing is reported until the whole
pipeline has proved itself with a TEST report. `bash infra/bootstrap.sh` walks through all of it.

1. **Bootstrap and apply.** The script creates the project, billing, state bucket,
   `terraform.tfvars` and `terraform init`. You run `terraform plan -out=tfplan` and
   `terraform apply tfplan` in `infra/envs/prod`.
   - If the first apply fails on the **alert policy** ("Cannot find metric"), the **Pub/Sub
     service agent**, or the **storage notification**, wait a minute and apply again. Brand-new
     projects take a moment to create those identities and metrics.
2. **Secrets.** The script stores:
   - the Gmail app password (the account needs **2-Step Verification on**);
   - your report details, including your timezone. These never touch the repo.
3. **Deploy the real report image.** Run the four `gh variable set …` lines the script prints,
   then re-run the *Report (build and deploy)* workflow on main.
4. **Enable delivery.** Set `report_push_enabled = true` in `terraform.tfvars` and apply. Until
   now the subscription only *kept* notifications, because the placeholder service would have
   swallowed them.
5. **One TEST report end to end.** Run `bash infra/send-test-violation.sh <bucket>`. You should
   get an email whose subject starts with **`[TEST] Barking dog complaint`**, with the PDF.
   - If nothing arrives, check `gcloud logging read 'resource.labels.service_name="report"' --freshness 15m`.
   - If Pub/Sub push is rejected before the app logs anything, set
     `report_ingress = "INGRESS_TRAFFIC_ALL"` and apply. IAM still lets only `chase-push` invoke it.
   - Click a recording link after about 12 hours to confirm the 7-day links work.
6. **Connect the OptiPlex, last.**
   1. The script creates `secrets/listener-key.json`.
   2. Run `bash infra/smoke-iam.sh <bucket> secrets/listener-key.json`. Every line should say PASS.
   3. On the OptiPlex, run `make violations`. **Every violation already in the outbox will be
      emailed** when uploads start, so move any you don't want reported out of `/data/outbox/`
      first.
   4. Copy the key to `~/chase/secrets/`, add `gcs_bucket: <bucket>` to `config/chase.yaml`, and
      run `make restart`.
7. **Confirm.** Run `make status` on the OptiPlex. `chase_outbox_pending` should drop to 0.

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
