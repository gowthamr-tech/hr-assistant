#!/bin/sh
# Choose the user the service runs as.
#
# - Started as non-root (e.g. Kubernetes runAsUser): run as that user, nothing to do.
# - Started as root, data folder owned by someone else: run as that owner, so SQLite can
#   write veltrix.db and its journal without changing the ownership of your files.
# - Started as root, data folder owned by root: drop to the built-in `app` user (10001) if it
#   can write there, otherwise stay root and say so. Fix with: chown -R 10001:10001 ./data
set -e
DB_FILE="${DB_PATH:-./data/veltrix.db}"
DATA_DIR="$(dirname "$DB_FILE")"
export HOME=/tmp

if [ "$(id -u)" = "0" ] && [ -d "$DATA_DIR" ]; then
  OWNER_UID="$(stat -c %u "$DATA_DIR")"
  OWNER_GID="$(stat -c %g "$DATA_DIR")"
  if [ "$OWNER_UID" != "0" ]; then
    exec setpriv --reuid="$OWNER_UID" --regid="$OWNER_GID" --clear-groups "$@"
  fi
  if setpriv --reuid=10001 --regid=10001 --clear-groups sh -c "test -w '$DATA_DIR' && test -w '$DB_FILE'" 2>/dev/null; then
    exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
  fi
  echo "WARNING: $DATA_DIR is owned by root and not writable by uid 10001; running as root." \
       "Run 'chown -R 10001:10001' on the data folder to run unprivileged." >&2
fi
exec "$@"
