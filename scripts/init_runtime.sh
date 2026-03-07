#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$ROOT_DIR/secrets"
chmod 700 "$ROOT_DIR/secrets"

if [[ ! -f "$ROOT_DIR/.env.runtime" ]]; then
  cp "$ROOT_DIR/.env.runtime.example" "$ROOT_DIR/.env.runtime"
  echo "created $ROOT_DIR/.env.runtime from template"
else
  echo "$ROOT_DIR/.env.runtime already exists"
fi

for secret_name in db_password db_root_password; do
  secret_path="$ROOT_DIR/secrets/$secret_name"
  if [[ ! -f "$secret_path" ]]; then
    : > "$secret_path"
    chmod 600 "$secret_path"
    echo "created empty secret file: $secret_path"
  else
    chmod 600 "$secret_path"
    echo "secret file already exists: $secret_path"
  fi
done

cat <<'EOF'

Next steps:
1. Edit .env.runtime
2. Fill secrets/db_password
3. Fill secrets/db_root_password
4. Run: gh auth login
5. Confirm: gh auth token returns a non-empty token
6. Run: python scripts/gh_token_compose.py up --build
EOF
