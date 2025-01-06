#!/bin/bash

# !!! WARNING DO NOT add -x to avoid leaking vault passwords
set -euo pipefail

source ~/.bash_profile

pyenv global $PYTHON_VERSION
echo "Python version:"
pyenv global

retry() {
  local retries=$1; shift
  local delay=$1; shift
  local attempts=1

  until "$@"; do
    retry_exit_status=$?
    echo "Exited with $retry_exit_status" >&2
    if (( retries == "0" )); then
      return $retry_exit_status
    elif (( attempts == retries )); then
      echo "Failed $attempts retries" >&2
      return $retry_exit_status
    else
      echo "Retrying $((retries - attempts)) more times..." >&2
      attempts=$((attempts + 1))
      sleep "$delay"
    fi
  done
}

vault_get() {
  key_path=${1:-}
  field=${2:-}

  if [[ -z "$field" || "$field" =~ ^-.* ]]; then
    retry 5 5 vault read "$key_path" "${@:2}"
  else
    retry 5 5 vault read -field="$field" "$key_path" "${@:3}"
  fi
}

is_pr_with_label() {
  match="$1"

  IFS=',' read -ra labels <<< "${GITHUB_PR_LABELS:-}"

  for label in "${labels[@]:-}"
  do
    if [ "$label" == "$match" ]; then
      return
    fi
  done

  false
}

is_auto_commit_disabled() {
  is_pr_with_label "ci:no-auto-commit"
}

make notice

if [ -z "$(git status --porcelain | grep NOTICE.txt)" ]; then
  exit 0
else 
  MACHINE_USERNAME="entsearchmachine"
  git config --global user.name "$MACHINE_USERNAME"
  git config --global user.email '90414788+entsearchmachine@users.noreply.github.com'
  GH_TOKEN=$(vault_get secret/jenkins-ci/swiftype-secrets/entsearchmachine github_token)

  if ! is_auto_commit_disabled; then
    gh pr checkout "${BUILDKITE_PULL_REQUEST}"
    git add NOTICE.txt
    git commit -m"Update NOTICE.txt"
    git push
    sleep 15
  fi

  exit 1
fi

exit 0
