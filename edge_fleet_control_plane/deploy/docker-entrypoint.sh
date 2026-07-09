#!/bin/sh
set -eu

echo "edge-control-plane entrypoint: arch=$(uname -m) python=$(python -V 2>&1)"

if [ -n "${DB_HOST:-}" ]; then
  echo "database: host=${DB_HOST} port=${DB_PORT:-5432} user=${DB_USER:-} db=${DB_NAME:-} sslmode=${DB_SSLMODE:-require}"
else
  echo "database: sqlite (DB_HOST unset)"
fi

python -c "import psycopg; import uvicorn; print('imports ok')" 2>&1 || {
  echo "FATAL: required Python packages missing"
  exit 1
}

if [ -n "${DB_HOST:-}" ]; then
  python - <<'PY' || exit 1
import os
import sys

host = os.environ["DB_HOST"]
port = int(os.environ.get("DB_PORT", "5432"))
user = os.environ.get("DB_USER", "")
password = os.environ.get("DB_PASSWORD", "")
dbname = os.environ.get("DB_NAME", user or "postgres")
sslmode = os.environ.get("DB_SSLMODE", "require")

if password in ("", "REPLACE_DB_PASSWORD"):
    print("FATAL: DB_PASSWORD is unset or still the placeholder REPLACE_DB_PASSWORD", file=sys.stderr)
    sys.exit(1)

try:
    import psycopg
    conninfo = (
        f"host={host} port={port} user={user} password={password} "
        f"dbname={dbname} sslmode={sslmode} connect_timeout=10"
    )
    with psycopg.connect(conninfo):
        pass
    print(f"database preflight ok ({host}:{port})")
except Exception as exc:
    print(f"FATAL: database preflight failed ({host}:{port}): {exc}", file=sys.stderr)
    print("If connection timed out, try DB_PORT=443 (Tapis inter-pod Postgres proxy).", file=sys.stderr)
    sys.exit(1)
PY
fi

exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8765 \
  --proxy-headers \
  --forwarded-allow-ips \
  "*"
