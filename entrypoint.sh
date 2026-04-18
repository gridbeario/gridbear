#!/bin/sh
# Fix ownership of bind-mounted volumes before dropping privileges.
# Users bind ./data, ./config, ./credentials from their host filesystem;
# those dirs are typically owned by a different uid than the gridbear user
# inside the container.

set -e

for dir in /app/data /app/config /app/credentials; do
    if [ -d "$dir" ]; then
        chown -R gridbear:gridbear "$dir" 2>/dev/null || true
    fi
done

# Ensure subdirs that plugins create on-demand exist with correct ownership
mkdir -p /app/data/attachments /app/data/models /app/data/avatars
chown -R gridbear:gridbear /app/data

exec gosu gridbear "$@"
