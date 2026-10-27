#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -d /etc/pve ]; then
  echo "Run App Hub inside a Linux VM, rather than on the Proxmox host."
  exit 1
fi
if command -v rpm >/dev/null 2>&1 && rpm -q podman-docker >/dev/null 2>&1; then
  echo "podman-docker is installed. App Hub requires Docker Engine, not the Podman compatibility command."
  echo "Resolve that package conflict and run: sudo sh install-docker.sh"
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is missing. In your Ubuntu/Debian/Fedora VM, run: sudo sh install-docker.sh"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Cannot access Docker. Start the Docker service and run this script with sudo."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "The Docker Compose plugin is missing. Run: sudo sh install-docker.sh"
  exit 1
fi
sh ./compose.sh up -d --build
echo
echo "App Hub is starting. Open http://YOUR-VM-IP:8080 to set up your owner password."
echo "If you changed APPHUB_PORT in .env, use that port instead."
echo "VM addresses: $(hostname -I 2>/dev/null || hostname)"
echo "First-run token: sudo sh compose.sh exec hub python -m apphub.server --show-setup-token"
echo "Service log: sudo sh compose.sh logs -f hub"
