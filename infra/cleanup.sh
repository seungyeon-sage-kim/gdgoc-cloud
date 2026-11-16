#!/usr/bin/env bash
# Remove everything this lab created, then check that nothing billable is left.
#
#   ./cleanup.sh                   terraform destroy + leftover check
#   ./cleanup.sh --unlink-billing  same, then turn billing off for the project
set -euo pipefail
cd "$(dirname "$0")"

tfvar() { sed -n "s/^$1 *= *\"\(.*\)\"/\1/p" terraform.tfvars; }
PROJECT=$(tfvar project_id)
REGION=$(tfvar region)
REGION=${REGION:-asia-northeast3}
[ -n "$PROJECT" ] || { echo "terraform.tfvars has no project_id"; exit 1; }

# Terraform signs in with Application Default Credentials; without them, borrow
# a one-hour token from the gcloud login
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
fi

if gcloud firestore databases describe --database='(default)' --project "$PROJECT" \
  --format='value(deleteProtectionState)' 2>/dev/null | grep -q ENABLED; then
  read -r -p "Firestore delete protection is on. Type yes to turn it off and continue: " answer
  [ "$answer" = yes ] || { echo "Stopped. Nothing was changed."; exit 1; }
  gcloud firestore databases update --database='(default)' --no-delete-protection --project "$PROJECT" --quiet
fi

echo "== 1. terraform destroy ($PROJECT)"
terraform destroy   # lists what will be deleted and waits for "yes"

echo
echo "== 2. leftovers that can still cost money"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
check() {
  local label=$1; shift
  local out
  # A check that fails says so: silence should mean nothing is left
  if ! out=$("$@" --project "$PROJECT" 2>&1 | sed -e '/^$/d' -e '/^Listed 0 items\.$/d' -e '/^Listing items under project/d'; exit "${PIPESTATUS[0]}"); then
    echo "-- $label: could not check (the API may be off): $(tail -1 <<<"$out")"
  elif [ -n "$out" ]; then
    echo "-- $label"; echo "$out"
  fi
}
check "Cloud Run services"  gcloud run services list --format="value(metadata.name,region)"
check "Cloud Run jobs"      gcloud run jobs list --format="value(metadata.name,region)"
check "buckets"             gcloud storage buckets list --format="value(name)"
check "image repositories"  gcloud artifacts repositories list --format="value(name.basename(),location)"
check "Firestore"           gcloud firestore databases list --format="value(name.basename(),locationId)"
check "Pub/Sub topics"      gcloud pubsub topics list --format="value(name.basename())"
check "Eventarc triggers"   gcloud eventarc triggers list --location="$REGION" --format="value(name.basename())"
check "Scheduler jobs"      gcloud scheduler jobs list --location="$REGION" --format="value(name.basename())"
check "secrets"             gcloud secrets list --format="value(name.basename())"
if [ -n "${LAB_DOWN:-}" ]; then
  # ./lab down deletes the state bucket next
  echo "(Anything listed above other than $PROJECT-tfstate failed to delete, or was not made by this lab.)"
else
  echo "(The bucket $PROJECT-tfstate is expected. Anything else listed above failed to delete, or was not made by this lab.)"
  echo "Terraform's state stays in gs://$PROJECT-tfstate; its old versions hold the old secret values."
  echo "  When you will not apply again:  gcloud storage rm -r gs://$PROJECT-tfstate"
fi

if [ "${1:-}" = "--unlink-billing" ]; then
  echo
  echo "== 3. billing off for $PROJECT"
  gcloud billing projects unlink "$PROJECT"
  echo "To stop the billing account itself: Billing → Account management → Close billing account."
fi

if [ -z "${LAB_DOWN:-}" ]; then
  echo
  echo "Note: the Workload Identity pool \"github\" stays soft-deleted for 30 days. To apply again before then:"
  echo "  gcloud iam workload-identity-pools undelete github --location=global --project=$PROJECT"
fi
