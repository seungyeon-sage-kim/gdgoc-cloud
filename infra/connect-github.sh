#!/usr/bin/env bash
# Give GitHub Actions what it needs to deploy: copies Terraform's outputs into
# the repository's Actions variables and turns deploys on. Nothing secret is
# stored in GitHub; the login is keyless (Workload Identity Federation), and
# terraform.tfvars holds no secrets.
#
#   ./connect-github.sh            uses the repository of the current git remote
#   ./connect-github.sh OWNER/REPO
set -euo pipefail
cd "$(dirname "$0")"

REPO=${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}

if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
fi

terraform output -json github_variables |
  python3 -c 'import json, sys; [print(k, v) for k, v in json.load(sys.stdin).items()]' |
  while read -r name value; do
    gh variable set "$name" --repo "$REPO" --body "$value"
    echo "set $name"
  done

# With production_extras, CI also applies infra/, and needs the same settings as this laptop
if terraform output -json github_variables | grep -q TERRAFORM_SA; then
  gh variable set TFVARS --repo "$REPO" < terraform.tfvars
  echo "set TFVARS. After changing terraform.tfvars, run this script again"
fi

gh variable set DEPLOY_ENABLED --repo "$REPO" --body true
echo "set DEPLOY_ENABLED=true. Deploy now with: gh workflow run ci.yml --ref main"
