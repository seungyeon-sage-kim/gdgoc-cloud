#!/usr/bin/env bash
# Time a few requests in a row and show which instance answered.
# The first request after the service has been idle is the cold start.
#
#   scripts/latency.sh https://web-xxxx.run.app/api/events/cloud-night/status 5
set -euo pipefail
URL=${1:?usage: latency.sh URL [COUNT]}
COUNT=${2:-5}
for i in $(seq 1 "$COUNT"); do
  curl -s -o /dev/null -D - -w 'total %{time_total}s\n' "$URL" |
    awk -v n="$i" 'tolower($1)=="x-served-by:"{who=$2} /^total/{printf "request %s  %s  %s\n", n, $2, who}'
done
