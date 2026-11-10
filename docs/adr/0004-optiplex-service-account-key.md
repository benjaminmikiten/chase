# 0004. The OptiPlex uploads with a service-account key

- **Status:** Accepted
- **Date:** 2026-11-10

## Decision

The listener on the OptiPlex authenticates to GCS with a key for a dedicated service account,
`chase-listener`. This is the one exception to "no service-account keys" (inherited from tower).

The account can **only create objects under `episodes/` and `violations/`** in the evidence bucket
(`roles/storage.objectCreator` with an IAM condition on the object name). It cannot read, list,
overwrite, delete, or touch `evidence/`.

The key is created by hand (`infra/bootstrap.sh`, with `gcloud iam service-accounts keys create`),
so the private key never enters Terraform state. It lives in `secrets/` on the OptiPlex, which is
git-ignored and mounted read-only into the container.

## Why

The OptiPlex is a machine in a house, not a GitHub Action or a Google workload, so keyless
federation isn't available to it. A leaked key could at worst add junk objects under two
prefixes. Those objects expire in 30 days, and a junk manifest only produces a report emailed to
Ben, never to anyone else.

## Operating it

- **Verify:** `infra/smoke-iam.sh <bucket> <key>` checks every allowed and denied action.
- **Rotate:** delete `secrets/listener-key.json`, re-run bootstrap stage 9, copy the new key to the
  OptiPlex, then delete the old key in the console (IAM → Service accounts → chase-listener → Keys).
