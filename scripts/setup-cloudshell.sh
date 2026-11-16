#!/usr/bin/env bash
# The tools this lab needs. Cloud Shell has no terraform or locust, and its gh and jq change
# without notice, so everyone gets the same versions. It already has gcloud, git, docker,
# python3 and uv.
#
# Installs fixed versions into ~/bin, which Cloud Shell keeps between sessions, and checks
# each download against the checksum its publisher lists. Safe to run again.
#
#   ./scripts/setup-cloudshell.sh
set -euo pipefail

TERRAFORM=1.9.8 # the version CI uses (.github/workflows/ci.yml)
GH=2.102.1
JQ=1.8.2
LOCUST=2.47.0 # the load test (loadtest/locustfile.py)

if [ "$(uname -s)-$(uname -m)" != Linux-x86_64 ]; then
  echo "This script is for Cloud Shell (Linux x86_64). Elsewhere, install the same versions yourself."
  exit 1
fi

BIN="$HOME/bin"
mkdir -p "$BIN"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# download URL NAME SUMS_URL: fetch a file and its publisher's checksum list, then compare
download() {
  curl -fsSL -o "$work/$2" "$1"
  curl -fsSL -o "$work/sums" "$3"
  (cd "$work" && grep "  $2\$" sums | sha256sum --check --status) || {
    echo "Checksum mismatch for $2: nothing installed" >&2
    exit 1
  }
}

# has NAME VERSION ARGS: is that version already in ~/bin?
has() {
  local out
  out=$("$BIN/$1" "${@:3}" 2> /dev/null) || return 1
  [[ $out == *"$2"* ]]
}

if ! has terraform "$TERRAFORM" version; then
  name=terraform_${TERRAFORM}_linux_amd64.zip
  download "https://releases.hashicorp.com/terraform/$TERRAFORM/$name" "$name" \
    "https://releases.hashicorp.com/terraform/$TERRAFORM/terraform_${TERRAFORM}_SHA256SUMS"
  python3 -m zipfile -e "$work/$name" "$work/terraform"
  install -m 755 "$work/terraform/terraform" "$BIN/terraform"
fi

if ! has gh "$GH" --version; then
  name=gh_${GH}_linux_amd64.tar.gz
  download "https://github.com/cli/cli/releases/download/v$GH/$name" "$name" \
    "https://github.com/cli/cli/releases/download/v$GH/gh_${GH}_checksums.txt"
  tar -xzf "$work/$name" -C "$work"
  install -m 755 "$work/gh_${GH}_linux_amd64/bin/gh" "$BIN/gh"
fi

if ! has jq "$JQ" --version; then
  download "https://github.com/jqlang/jq/releases/download/jq-$JQ/jq-linux-amd64" jq-linux-amd64 \
    "https://github.com/jqlang/jq/releases/download/jq-$JQ/sha256sum.txt"
  install -m 755 "$work/jq-linux-amd64" "$BIN/jq"
fi

# Python tools go through uv, which keeps them in ~/.local/bin
uv tool install --quiet "locust==$LOCUST"

# New shells find both folders
for dir in '$HOME/bin' '$HOME/.local/bin'; do
  grep -qF "PATH=\"$dir:" ~/.bashrc 2> /dev/null || echo "export PATH=\"$dir:\$PATH\"" >> ~/.bashrc
done
export PATH="$BIN:$HOME/.local/bin:$PATH"

terraform version | head -1
gh --version | head -1
jq --version
locust --version
echo "Ready. Open a new Cloud Shell tab, or run: source ~/.bashrc"
