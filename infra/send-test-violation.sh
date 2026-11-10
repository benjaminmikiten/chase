#!/usr/bin/env bash
# Send one end-to-end TEST report through the real pipeline, using synthetic data.
#
#   bash infra/send-test-violation.sh <evidence-bucket>
#
# Uploads the synthetic fixture episodes (report/tests/fixtures/bucket) with your own gcloud
# login, then a manifest marked "test": true under a recognizably fake violation id. The report
# service emails you a report whose subject starts with "[TEST]". Re-running sends another
# (each run uses a new revision number).
set -euo pipefail

BUCKET=${1:?usage: send-test-violation.sh <bucket>}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIX="$ROOT/report/tests/fixtures/bucket"
VID="v-19991231T235959Z"   # can't collide with a real violation
REV=$(date +%s)

command -v jq >/dev/null || { echo "needs jq"; exit 1; }
for d in "$FIX"/episodes/*/; do
  id=$(basename "$d")
  gcloud storage cp --quiet "$d/audio.flac" "$d/metadata.json" "gs://$BUCKET/episodes/$id/"
done
MANIFEST=$(mktemp)
trap 'rm -f "$MANIFEST"' EXIT
jq --arg vid "$VID" --argjson rev "$REV" '.violation_id=$vid | .revision=$rev | .test=true' \
  "$FIX"/violations/*/rev-1.json > "$MANIFEST"
gcloud storage cp --quiet "$MANIFEST" "gs://$BUCKET/violations/$VID/rev-$REV.json"
echo "Uploaded test violation $VID rev $REV."
echo "Expect an email with subject starting '[TEST] Barking dog complaint' within a couple of minutes."
echo "Logs: gcloud logging read 'resource.labels.service_name=\"report\"' --limit 20 --freshness 10m"
