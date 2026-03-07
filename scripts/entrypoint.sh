#!/usr/bin/env bash
set -euo pipefail

load_secret_var() {
  local var_name="$1"
  local file_var_name="${var_name}_FILE"
  local current_value="${!var_name:-}"
  local file_path="${!file_var_name:-}"

  if [[ -n "$current_value" && -n "$file_path" ]]; then
    echo "refusing to start: both $var_name and $file_var_name are set" >&2
    exit 1
  fi

  if [[ -n "$file_path" ]]; then
    if [[ ! -r "$file_path" ]]; then
      echo "refusing to start: secret file for $var_name is not readable: $file_path" >&2
      exit 1
    fi
    export "$var_name=$(tr -d '\r\n' < "$file_path")"
  fi
}

append_missing_var() {
  local var_name="$1"
  if [[ -z "${!var_name:-}" ]]; then
    missing_vars+=("$var_name")
  fi
}

missing_vars=()

load_secret_var DB_PASSWORD
load_secret_var GITHUB_TOKEN
load_secret_var COPILOT_API_KEY

IFS=',' read -r -a required_env_vars <<< "${REQUIRED_ENV_VARS:-DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD}"
for var_name in "${required_env_vars[@]}"; do
  var_name="${var_name// /}"
  [[ -n "$var_name" ]] || continue
  append_missing_var "$var_name"
done

service_name=""
if [[ $# -ge 3 && "$1" == "python" && "$2" == "scripts/service_runner.py" ]]; then
  service_name="$3"
fi

case "$service_name" in
  brain)
    append_missing_var TARGET_REPO
    append_missing_var TARGET_REF
    append_missing_var COPILOT_CONFIG_DIR
    if [[ -z "${GITHUB_TOKEN:-}" && -z "${COPILOT_API_KEY:-}" ]]; then
      missing_vars+=("GITHUB_TOKEN|COPILOT_API_KEY")
    fi
    ;;
  guardian)
    append_missing_var COPILOT_CONFIG_DIR
    if [[ -z "${GITHUB_TOKEN:-}" && -z "${COPILOT_API_KEY:-}" ]]; then
      missing_vars+=("GITHUB_TOKEN|COPILOT_API_KEY")
    fi
    ;;
esac

if (( ${#missing_vars[@]} > 0 )); then
  echo "refusing to start: missing required environment variables: ${missing_vars[*]}" >&2
  exit 1
fi

exec "$@"