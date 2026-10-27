#!/bin/sh
# Install official Docker Engine and Compose in an Ubuntu, Debian, or Fedora VM.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo inside your Linux VM."
  exit 1
fi
if [ -d /etc/pve ]; then
  echo "Create an Ubuntu, Debian, or Fedora VM in Proxmox and run this installer inside it."
  exit 1
fi
. /etc/os-release
case "$ID" in
  ubuntu|debian|fedora) ;;
  *) echo "This installer supports Ubuntu, Debian, and Fedora. Install Docker Engine and Compose for your distribution."; exit 1 ;;
esac
if [ "$ID" = fedora ] && [ -e /run/ostree-booted ]; then
  echo "Use a standard Fedora Server/Workstation VM. This installer does not modify an rpm-ostree system."
  exit 1
fi
if [ "$ID" = fedora ] && rpm -q podman-docker >/dev/null 2>&1; then
  echo "Found podman-docker, which replaces the Docker command with Podman."
  echo "App Hub needs Docker Engine. Resolve that package conflict before rerunning this installer."
  echo "Standalone Podman can remain installed. No packages were removed."
  exit 1
fi
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker and Compose are already installed."
  exit 0
fi
if [ "$ID" = fedora ]; then
  for package in docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-selinux docker-engine-selinux docker-engine docker-cli moby-engine moby-cli containerd runc; do
    if rpm -q "$package" >/dev/null 2>&1; then
      echo "Found existing container package: $package"
      echo "Keep your existing setup, or follow Docker's Fedora migration instructions."
      echo "This fresh-VM installer will not remove existing container software."
      exit 1
    fi
  done
  if command -v dnf5 >/dev/null 2>&1; then
    dnf5 -y install ca-certificates dnf5-plugins
    dnf5 config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo --overwrite
    dnf5 -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    dnf -y install ca-certificates dnf-plugins-core
    dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
    dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
else
  for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    installed=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
    case "$installed" in
      'install ok installed')
        echo "Found existing distribution package: $package"
        echo "Keep your existing Docker setup, or follow the official Docker migration instructions."
        echo "This fresh-VM installer will not remove existing container software."
        exit 1
        ;;
    esac
  done
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename=${UBUNTU_CODENAME:-$VERSION_CODENAME}
  architecture=$(dpkg --print-architecture)
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $codename
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
echo "Docker is ready. Next: sudo sh start.sh"
