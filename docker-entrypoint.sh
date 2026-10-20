#!/bin/sh
# Run as the owner of the mounted data folder so SQLite can write to veltrix.db and its
# journal, without changing the ownership of the evaluator's files. Root-owned data keeps
# root; anything else runs unprivileged.
set -e
DATA_DIR="$(dirname "${DB_PATH:-./data/veltrix.db}")"
if [ "$(id -u)" = "0" ] && [ -d "$DATA_DIR" ]; then
  OWNER_UID="$(stat -c %u "$DATA_DIR")"
  OWNER_GID="$(stat -c %g "$DATA_DIR")"
  if [ "$OWNER_UID" != "0" ]; then
    export HOME=/tmp
    exec setpriv --reuid="$OWNER_UID" --regid="$OWNER_GID" --clear-groups "$@"
  fi
fi
exec "$@"
