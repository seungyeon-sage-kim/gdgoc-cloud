#!/usr/bin/env bash
# The production deploy. .github/workflows/ci.yml runs one stage per step.
#
#   record    note what is serving now, so it can be put back
#   stage     start the new ticket-worker, waitlist-promoter and web with 0% traffic.
#             A revision that fails its startup probe stops the deploy here.
#   smoke     call web's new revision at its own URL
#   switch    move to the new version: receivers first, then the job, then web
#   verify    call the live URL
#   rollback  if any stage failed: put back what record noted
#
# Users and messages reach no new code before switch, so a failure up to there
# changes nothing. Only the image changes here: Terraform owns the settings
# (infra/run.tf). Keep message formats backward compatible, because during switch
# a new worker can receive a message from an old sender, and after a rollback an
# old worker can receive one from a new sender.
#
# Needs: REGION, IMAGES (the registry path), WEB_SERVICE, GITHUB_SHA.

set -euo pipefail

STATE="${RUNNER_TEMP:-/tmp}/deploy-previous"

# The revisions taking traffic, as gcloud's --to-revisions wants them: name=100
# or, during a canary, name=90,other=10
serving() {
  gcloud run services describe "$1" --region "$REGION" --format json |
    jq -er '[.status.traffic[] | select(.percent > 0) | "\(.revisionName)=\(.percent)"] | join(",") | select(. != "")'
}

record() {
  local web promoter worker job
  web=$(serving "$WEB_SERVICE")
  promoter=$(serving waitlist-promoter)
  worker=$(serving ticket-worker)
  job=$(gcloud run jobs describe housekeeping --region "$REGION" --format json |
    jq -er '.spec.template.spec.template.spec.containers[0].image')
  # In the order rollback goes through them: senders first, the reverse of switch
  printf '%s %s\n' "$WEB_SERVICE" "$web" housekeeping "$job" waitlist-promoter "$promoter" ticket-worker "$worker" > "$STATE"
  cat "$STATE"
}

stage() {
  local worker
  for worker in ticket-worker waitlist-promoter; do
    gcloud run services update "$worker" --quiet --region "$REGION" --image "$IMAGES/$worker:$GITHUB_SHA" --no-traffic
  done
  gcloud run services update "$WEB_SERVICE" --quiet --region "$REGION" --image "$IMAGES/web:$GITHUB_SHA" --no-traffic --tag candidate
}

# Needs a public web (allow_public_web = true, the default); a private one would need an identity
# token here. The workers accept only internal traffic, so CI cannot call them: their
# startup probe is the check that they start, and the tests cover how they work.
smoke() {
  local url
  url=$(gcloud run services describe "$WEB_SERVICE" --region "$REGION" --format json |
    jq -er '.status.traffic[] | select(.tag=="candidate") | .url')
  echo "candidate: $url"
  curl -fsS "$url/health"
  curl -fsS -o /dev/null "$url/" # the homepage reads the events from Firestore
}

# --to-latest (not --to-tags) so later revisions, e.g. a Terraform setting change, get traffic too.
switch() {
  local worker
  for worker in ticket-worker waitlist-promoter; do
    gcloud run services update-traffic "$worker" --quiet --region "$REGION" --to-latest
  done
  # A job has no revisions or traffic, so its new image can only be tried by running
  # it. --check makes the job's reads and no writes: a deploy never sends reminders
  # or releases seats. The next Scheduler run does the real work (jobs/housekeeping.py).
  gcloud run jobs update housekeeping --quiet --region "$REGION" --image "$IMAGES/housekeeping:$GITHUB_SHA"
  gcloud run jobs execute housekeeping --quiet --region "$REGION" --args=--check --wait
  gcloud run services update-traffic "$WEB_SERVICE" --quiet --region "$REGION" --to-latest
}

verify() {
  local url
  url=$(gcloud run services describe "$WEB_SERVICE" --region "$REGION" --format 'value(status.url)')
  echo "url=$url" >> "${GITHUB_OUTPUT:-/dev/null}"
  curl -fsS "$url/health"
  curl -fsS -o /dev/null "$url/"
}

# Tries everything even if one step fails, so as much as possible goes back. The failed
# revisions stay, with no traffic: web's is still at its candidate URL to look at.
rollback() {
  local name previous failed=0
  if [ ! -f "$STATE" ]; then
    echo "Nothing recorded, so nothing moved."
    return 0
  fi
  while read -r name previous <&3; do
    echo "Putting $name back on $previous"
    if [ "$name" = housekeeping ]; then
      gcloud run jobs update housekeeping --quiet --region "$REGION" --image "$previous" || failed=1
    elif ! gcloud run services update-traffic "$name" --quiet --region "$REGION" --to-revisions "$previous"; then
      # When the new revision never started (for example, its container listens on the wrong
      # port), Cloud Run reports the service as not ready until the next good deploy, so this
      # command fails although no traffic moved. What counts is that the previous revision
      # still serves everything.
      if gcloud run services describe "$name" --region "$REGION" --format json |
        jq -e --arg rev "${previous%%=*}" '.status.traffic[] | select(.revisionName == $rev and .percent == 100)' >/dev/null; then
        echo "$name still serves ${previous%%=*} at 100%; the failed revision never received traffic."
      else
        failed=1
      fi
    fi
  done 3< "$STATE"
  return "$failed"
}

case "${1:-}" in
  record | stage | smoke | switch | verify | rollback) "$1" ;;
  *)
    echo "usage: $0 record|stage|smoke|switch|verify|rollback" >&2
    exit 2
    ;;
esac
