#!/bin/sh
# Use this wrapper for every Compose operation, including logs and recovery.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
selinux_state=Disabled
if command -v getenforce >/dev/null 2>&1; then
  selinux_state=$(getenforce 2>/dev/null || true)
fi
if [ -e /sys/fs/selinux/enforce ]; then
  selinux_state=Enabled
fi
case "$selinux_state" in
  Enforcing|Permissive|Enabled)
    exec docker compose -f compose.yaml -f compose.selinux.yaml "$@"
    ;;
  *) exec docker compose -f compose.yaml "$@" ;;
esac
