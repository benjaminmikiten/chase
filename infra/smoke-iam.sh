#!/usr/bin/env bash
# Prove the listener key can do exactly what ADR 0004 says and nothing more.
#
#   bash infra/smoke-iam.sh <evidence-bucket> <path/to/listener-key.json>
#
# Expected: create under episodes/ and violations/ succeeds; create under evidence/, read, and
# list are all denied. Uses a throwaway gcloud config, so your own login is untouched. The two test
# objects it creates are deleted by the bucket's 30-day lifecycle rule (the key can't delete
# them, by design). The violations/ object isn't a manifest name, so the report service ignores it.
set -uo pipefail

BUCKET=${1:?usage: smoke-iam.sh <bucket> <key.json>}
KEY=${2:?usage: smoke-iam.sh <bucket> <key.json>}
CLOUDSDK_CONFIG=$(mktemp -d)
export CLOUDSDK_CONFIG
trap 'rm -rf "$CLOUDSDK_CONFIG" "$TMPF"' EXIT
TMPF=$(mktemp)
echo "chase IAM smoke test $(date -u +%FT%TZ)" > "$TMPF"
gcloud auth activate-service-account --key-file="$KEY" --quiet >/dev/null 2>&1 || { echo "FAIL: key not accepted"; exit 1; }

ID="_smoke-$(date -u +%Y%m%dT%H%M%SZ)"
fail=0
expect() { # expect allow|deny "description" command...
  local want=$1 what=$2; shift 2
  if "$@" >/dev/null 2>&1; then got=allow; else got=deny; fi
  if [[ $got == "$want" ]]; then echo "PASS  $what ($got)"; else echo "FAIL  $what (wanted $want, got $got)"; fail=1; fi
}
expect allow "create episodes/"   gcloud storage cp "$TMPF" "gs://$BUCKET/episodes/$ID/smoke.txt"
expect allow "create violations/" gcloud storage cp "$TMPF" "gs://$BUCKET/violations/$ID.txt"
expect deny  "create evidence/"   gcloud storage cp "$TMPF" "gs://$BUCKET/evidence/$ID.txt"
expect deny  "read an object"     gcloud storage cat "gs://$BUCKET/episodes/$ID/smoke.txt"
expect deny  "list the bucket"    gcloud storage ls "gs://$BUCKET/"
expect deny  "overwrite (delete)" gcloud storage cp "$TMPF" "gs://$BUCKET/episodes/$ID/smoke.txt"
exit $fail
