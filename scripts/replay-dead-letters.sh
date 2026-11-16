#!/usr/bin/env bash
# Put failed messages back on the topic they came from, once the cause is fixed
# (the mail provider was down, for example). Run it with your own gcloud login.
#
#   scripts/replay-dead-letters.sh            show what is waiting
#   scripts/replay-dead-letters.sh --replay   publish each one again, then remove it
#
# Repeats are safe: the worker is idempotent and each email goes out once. To throw
# everything away instead: gcloud pubsub subscriptions seek dead-letter --time="$(date -u +%FT%TZ)"
set -euo pipefail

# quiet COMMAND...: run it, and show what it printed only if it failed
quiet() {
  local out
  out=$("$@" 2>&1) || { echo "$out" >&2; return 1; }
}

# 10 at a time: each must be replayed before its 60-second acknowledgement deadline passes
errors=$(mktemp)
trap 'rm -f "$errors"' EXIT
pull() {
  waiting=$(gcloud pubsub subscriptions pull dead-letter --limit 10 --format json 2>"$errors") || {
    cat "$errors" >&2
    exit 1
  }
  count=$(jq length <<<"${waiting:-[]}")
}

pull
if [ "$count" = 0 ]; then
  echo "0 messages waiting."
  echo "If you just pulled a message in the Console (Pull), it stays hidden until its acknowledgement"
  echo "deadline passes. Try again in a minute."
  exit 0
fi

if [ "${1:-}" != "--replay" ]; then
  echo "$count messages waiting (at most 10 shown)"
  jq -r '.[].message | "\(.attributes.CloudPubSubDeadLetterSourceSubscription)\t\(.publishTime)"' <<<"$waiting"
  # Hand them straight back, so nothing waits for the pull to expire
  jq -r '.[].ackId' <<<"$waiting" | while read -r id; do
    quiet gcloud pubsub subscriptions modify-message-ack-deadline dead-letter --ack-ids "$id" --ack-deadline 0
  done
  exit 0
fi

# One message per line. A message is removed only after it was published again.
while [ "$count" != 0 ]; do
  jq -c '.[]' <<<"$waiting" | while read -r item; do
    case $(jq -r '.message.attributes.CloudPubSubDeadLetterSourceSubscription' <<<"$item") in
      mailer) topic=emails ;;
      ticket-worker) topic=registrations ;;
      *) echo "unknown source, left in place: $(jq -r .message.messageId <<<"$item")"; continue ;;
    esac
    # gcloud prints URL-safe base64, which jq cannot read until it is put back to the standard form
    body=$(jq -r '.message.data | gsub("-";"+") | gsub("_";"/") | @base64d' <<<"$item")
    quiet gcloud pubsub topics publish "$topic" --message "$body"
    quiet gcloud pubsub subscriptions ack dead-letter --ack-ids "$(jq -r .ackId <<<"$item")"
    echo "replayed to $topic"
  done
  pull
done
