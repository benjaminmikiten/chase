#!/usr/bin/env bash
# chase cloud bootstrap: the one-time manual steps around `terraform apply`, as a guided script.
#
#   bash infra/bootstrap.sh            # walk through every stage (safe to re-run)
#   bash infra/bootstrap.sh --help
#
# What it does, in order (each stage asks before changing anything and skips what exists):
#   1. checks prerequisites (gcloud, terraform 1.5.7, gh, jq)
#   2. creates or selects the GCP project            5. writes infra/envs/prod/terraform.tfvars
#   3. links billing                                  6. terraform init
#   4. creates the versioned Terraform state bucket   7. YOU run terraform plan / apply
#   8. stores the two secrets (Gmail app password, report config JSON) in Secret Manager
#   9. creates the listener's service-account key in ./secrets/ (never in Terraform state)
#  10. prints the `gh variable set` commands for the deploy workflow (it does not run them)
#
# It never runs `terraform apply`, and it remembers non-secret answers in
# ~/.config/chase-bootstrap/env.
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/infra/envs/prod"
STATE="${XDG_CONFIG_HOME:-$HOME/.config}/chase-bootstrap"
mkdir -p "$STATE"
touch "$STATE/env"
# shellcheck disable=SC1091
source "$STATE/env"

bold=$(tput bold 2>/dev/null || true); dim=$(tput dim 2>/dev/null || true); reset=$(tput sgr0 2>/dev/null || true)
stage() { printf '\n%s▸ %s%s\n' "$bold" "$1" "$reset"; }
say() { printf '  %s\n' "$1"; }
remember() { grep -v "^$1=" "$STATE/env" > "$STATE/env.tmp" || true; echo "$1=\"$2\"" >> "$STATE/env.tmp"; mv "$STATE/env.tmp" "$STATE/env"; }
ask() { # ask VAR "prompt" [default]
  local var=$1 prompt=$2 def=${3:-${!1:-}} ans
  read -r -p "  $prompt${def:+ [$def]}: " ans
  printf -v "$var" '%s' "${ans:-$def}"
}
confirm() { local ans; read -r -p "  $1 [y/N] " ans; [[ "$ans" =~ ^[Yy] ]]; }

stage "1/10 Prerequisites"
for cmd in gcloud terraform gh jq; do
  if command -v "$cmd" >/dev/null; then say "✓ $cmd"; else say "✗ $cmd missing"; MISSING=1; fi
done
if [[ -n "${MISSING:-}" ]]; then
  say "macOS: brew install --cask gcloud-cli && brew install tfenv gh jq && tfenv install 1.5.7 && tfenv use 1.5.7"
  exit 1
fi
terraform version | head -1 | grep -q 'v1.5.7' || say "${dim}(terraform isn't 1.5.7; CI pins 1.5.7)${reset}"

stage "2/10 GCP project"
ask PROJECT_ID "Project ID (new or existing, globally unique)" "${PROJECT_ID:-chase-$(whoami)}"
remember PROJECT_ID "$PROJECT_ID"
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  say "✓ project $PROJECT_ID exists"
elif confirm "Create project $PROJECT_ID?"; then
  gcloud projects create "$PROJECT_ID"
fi

stage "3/10 Billing"
if [[ "$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null)" == "True" ]]; then
  say "✓ billing linked"
else
  gcloud billing accounts list
  ask BILLING_ACCOUNT "Billing account ID to link"
  remember BILLING_ACCOUNT "$BILLING_ACCOUNT"
  confirm "Link $BILLING_ACCOUNT to $PROJECT_ID?" && gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
fi

stage "4/10 Terraform state bucket"
ask REGION "Region" "${REGION:-us-central1}"
remember REGION "$REGION"
STATE_BUCKET="${STATE_BUCKET:-$PROJECT_ID-tfstate}"
ask STATE_BUCKET "State bucket name" "$STATE_BUCKET"
remember STATE_BUCKET "$STATE_BUCKET"
gcloud auth application-default print-access-token >/dev/null 2>&1 || gcloud auth application-default login
gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com storage.googleapis.com --project="$PROJECT_ID"
gcloud auth application-default set-quota-project "$PROJECT_ID" >/dev/null 2>&1 || true
if gcloud storage buckets describe "gs://$STATE_BUCKET" >/dev/null 2>&1; then
  say "✓ gs://$STATE_BUCKET exists"
elif confirm "Create gs://$STATE_BUCKET (private, versioned)?"; then
  gcloud storage buckets create "gs://$STATE_BUCKET" --project="$PROJECT_ID" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
  gcloud storage buckets update "gs://$STATE_BUCKET" --versioning
fi

stage "5/10 terraform.tfvars (git-ignored)"
if [[ -f "$TF_DIR/terraform.tfvars" ]]; then
  say "✓ exists (edit $TF_DIR/terraform.tfvars to change)"
else
  ask ALERT_EMAIL "Email for cloud alerts" "${ALERT_EMAIL:-}"
  remember ALERT_EMAIL "$ALERT_EMAIL"
  ask GH_REPO "GitHub repo (owner/name)" "${GH_REPO:-benjaminmikiten/chase}"
  remember GH_REPO "$GH_REPO"
  cat > "$TF_DIR/terraform.tfvars" <<EOF
project_id        = "$PROJECT_ID"
region            = "$REGION"
alert_email       = "$ALERT_EMAIL"
github_repository = "$GH_REPO"
EOF
  say "✓ wrote $TF_DIR/terraform.tfvars"
fi

stage "6/10 terraform init"
(cd "$TF_DIR" && terraform init -input=false -backend-config="bucket=$STATE_BUCKET")

stage "7/10 Plan and apply (you run these)"
say "In another terminal:"
say "  cd $TF_DIR && terraform plan -out=tfplan   # read it"
say "  terraform apply tfplan"
say "First apply takes ~5 minutes. Come back here when it has finished."
confirm "Has terraform apply finished successfully?" || { say "Re-run this script after applying."; exit 0; }
OUT() { (cd "$TF_DIR" && terraform output -raw "$1"); }

stage "8/10 Secrets (Secret Manager, never the repo)"
say "Gmail app password: Google Account → Security → 2-Step Verification (must be ON) → App passwords."
if confirm "Set or replace the Gmail app password now?"; then
  read -r -s -p "  App password (16 letters, input hidden): " APP_PW; echo
  printf '%s' "${APP_PW// /}" | gcloud secrets versions add chase-gmail-app-password --project="$PROJECT_ID" --data-file=-
  unset APP_PW
fi
if confirm "Set or replace the report details (name, address, phone, emails, neighbor addresses)?"; then
  ask R_NAME "Your name (signs the letter)"
  ask R_ADDR "Your address (one line)"
  ask R_PHONE "Your phone"
  ask R_EMAIL "Gmail address that sends the report (the app password's account)"
  ask R_TO "Where reports are sent (usually the same address)" "$R_EMAIL"
  ask R_NEIGHBORS "Neighboring addresses to list, separated by ';' (may be empty)"
  jq -n --arg n "$R_NAME" --arg a "$R_ADDR" --arg p "$R_PHONE" --arg e "$R_EMAIL" --arg t "$R_TO" --arg nb "$R_NEIGHBORS" \
    '{name:$n,address:$a,phone:$p,email:$e,to_email:$t,
      neighbor_addresses: ($nb | split(";") | map(gsub("^\\s+|\\s+$";"")) | map(select(length>0)))}' \
    | gcloud secrets versions add chase-report-config --project="$PROJECT_ID" --data-file=-
fi

stage "9/10 Listener key (create-only; ADR 0004)"
KEY="$ROOT/secrets/listener-key.json"
if [[ -f "$KEY" ]]; then
  say "✓ $KEY exists (delete it and re-run to rotate; remove old keys in the console)"
elif confirm "Create a key for $(OUT listener_service_account) into $KEY?"; then
  mkdir -p "$ROOT/secrets" && chmod 700 "$ROOT/secrets"
  gcloud iam service-accounts keys create "$KEY" --iam-account="$(OUT listener_service_account)" --project="$PROJECT_ID"
  chmod 600 "$KEY"
fi
say "Copy it to the OptiPlex:"
say "  scp $KEY wizard@orb:~/chase/secrets/listener-key.json"
say "Then on the OptiPlex, in config/chase.yaml:  gcs_bucket: $(OUT evidence_bucket)   and run: make restart"
say "Check it:  bash infra/smoke-iam.sh $(OUT evidence_bucket) $KEY"

stage "10/10 GitHub variables for the deploy workflow (copy and run)"
cat <<EOF
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "$(OUT workload_identity_provider)"
  gh variable set GCP_SERVICE_ACCOUNT --body "$(OUT ci_service_account)"
  gh variable set GCP_ARTIFACT_REGISTRY_URL --body "$(OUT artifact_registry_url)"
  gh variable set GCP_REGION --body "$(OUT region)"
EOF
say "Then re-run the 'Report (build and deploy)' workflow, or push to main, to deploy the real image."
say "Finally, send one TEST report:  bash infra/send-test-violation.sh $(OUT evidence_bucket)"
printf '\n%sDone.%s\n' "$bold" "$reset"
