# 0005. Report trigger and exactly-once email

- **Status:** Accepted
- **Date:** 2026-11-10

## Decision

- **Trigger:** a GCS notification for objects under `violations/` publishes to the
  `chase-violations` Pub/Sub topic. A push subscription calls the `report` Cloud Run service with
  an OIDC token. The design doc named Eventarc; Pub/Sub push replaces it because it gives:
  - prefix filtering;
  - an explicit **600 s ack deadline**, matching the service timeout;
  - exponential retry (10 s to 600 s);
  - a **dead-letter topic** after 10 attempts, which alerts.
- **Exactly one email per manifest revision**, despite at-least-once delivery:
  1. Only the **highest** revision present is processed; older ones are "stale".
  2. The service **claims** a revision by creating `evidence/<vid>/rev-<n>/claimed.json` with
     `if_generation_match=0` *before* any work. The delivery that loses the race exits 204.
  3. On failure before sending, the claim is released and the request returns 500, so Pub/Sub
     retries. On failure *after* sending, the claim is kept and `report_failed_after_send` is
     logged, so a retry never sends twice.
  4. **Max 1 instance, concurrency 1.**
- **Missing evidence:** if a manifest's episodes aren't in the bucket yet, the service returns 500
  and sends nothing. The listener's uploader also uploads every episode before its manifest.
- **Bounded memory:** Cloud Run `/tmp` is RAM, so episodes are processed one at a time, ffmpeg
  works file to file, and temp files are deleted as it goes. The service has 4 GiB.

## Consequences

- A retried delivery can at worst duplicate evidence copies, never emails.
- A report can be re-sent deliberately by uploading a new revision of the manifest
  (`infra/send-test-violation.sh` does this for tests).
