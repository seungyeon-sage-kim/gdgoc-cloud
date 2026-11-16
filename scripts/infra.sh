#!/usr/bin/env bash
# Terraform in CI. .github/workflows/ci.yml runs one stage per step, before the deploy,
# because new code may need a new setting or permission.
#
#   changes  does this run apply? Only if infra/ differs from the last run that succeeded,
#            or a person started the run. Comparing with the last success, not the last
#            push, means a failed apply is tried again by the next push.
#   plan     work out what would change, and save it
#   check    stop if that plan destroys or replaces anything, unless a person started
#            this run with allow_destroy after reading the plan of the run that stopped
#   apply    apply exactly what was saved
#
# Not every push applies: gcloud names the web revision it tags as the candidate, so after
# any deploy the plan shows `revision -> null`, and applying that only adds a revision of
# the same image.
#
# A failed deploy rolls back the code, not these settings, so a change here must also
# work with the version already running. A plan that destroys something needs a person:
# read it in the stopped run, then gh workflow run ci.yml --ref main -f allow_destroy=true
#
# Needs: PROJECT_ID, TFVARS (infra/connect-github.sh sets it), and GH_TOKEN for changes.
# ALLOW_DESTROY is "true" only in a run a person started with allow_destroy.

set -euo pipefail

cd "$(dirname "$0")/../infra"

changes() {
  local last
  last=$(gh run list --workflow ci.yml --branch main --status success --limit 1 --json headSha --jq '.[0].headSha // ""')
  if [ "$GITHUB_EVENT_NAME" != workflow_dispatch ] &&
    [ -n "$last" ] && git cat-file -e "$last^{commit}" 2> /dev/null &&
    git diff --quiet "$last" HEAD -- .; then
    echo "infra=false" >> "$GITHUB_OUTPUT"
    echo "infra/ has not changed since $last. Nothing to apply."
  else
    echo "infra=true" >> "$GITHUB_OUTPUT"
    echo "Applying infra/. Last successful run: ${last:-none}"
  fi
}

plan() {
  printf '%s\n' "$TFVARS" > terraform.tfvars
  terraform init -input=false -backend-config="bucket=$PROJECT_ID-tfstate" -backend-config="prefix=gdgoc-tickets"
  terraform plan -input=false -no-color -out=tfplan
}

# A replacement is a delete plus a create, so it counts
check() {
  local destroyed
  destroyed=$(terraform show -json tfplan | jq -r '.resource_changes[]? | select(.change.actions | index("delete")) | .address')
  if [ -z "$destroyed" ]; then
    echo "This plan destroys nothing."
  elif [ "${ALLOW_DESTROY:-}" = true ]; then
    echo "::warning::This plan destroys or replaces resources. A person allowed it (allow_destroy):"
    echo "$destroyed"
  else
    echo "::error::This plan destroys or replaces resources, so CI stops. Read the plan, then run: gh workflow run ci.yml --ref main -f allow_destroy=true"
    echo "$destroyed"
    exit 1
  fi
}

apply() {
  terraform apply -input=false -no-color tfplan
}

case "${1:-}" in
  changes | plan | check | apply) "$1" ;;
  *)
    echo "usage: $0 changes|plan|check|apply" >&2
    exit 2
    ;;
esac
