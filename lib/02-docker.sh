#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    lib/02-docker.sh
# Purpose: Install Docker from Docker's OFFICIAL repository, then relocate
#          BOTH Docker's data-root AND containerd's root to the NVMe.
#          Also installs ripgrep and sqlite3 for the Web UI.
#
# Why the official repo?  Raspberry Pi OS ships an older `docker.io` and does
#          NOT include the `docker-compose-plugin` package at all. Docker's
#          own repo has the current Engine, Buildx, and Compose plugin.
# =============================================================================

install_docker() {
  # ---------- base packages ----------
  log "Updating apt and installing base packages ..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    ca-certificates curl gnupg git python3 python3-venv python3-pip \
    jq parted e2fsprogs zram-tools cmake build-essential ripgrep sqlite3

  # ---------- add Docker's official GPG key + repository ----------
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    log "Adding Docker's official GPG key and repository ..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg \
      -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable
EOF

    apt-get update -qq
  else
    info "Docker repository already configured — skipping."
  fi

  # ---------- install Docker + Compose plugin ----------
  if ! command -v docker &>/dev/null; then
    log "Installing Docker Engine + Compose plugin ..."
    apt-get install -y -qq \
      docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin
  else
    info "Docker already installed — skipping."
    if ! docker compose version &>/dev/null; then
      log "Installing Docker Compose plugin ..."
      apt-get install -y -qq docker-compose-plugin
    fi
  fi

  systemctl enable --now docker containerd

  usermod -aG docker "$REAL_USER" 2>/dev/null || warn "Could not add ${REAL_USER} to docker group"
  info "User ${REAL_USER} added to docker group — log out and back in."

  # ---------- relocate Docker to NVMe ----------
  log "Relocating Docker data-root to NVMe ..."
  systemctl stop docker containerd 2>/dev/null || true

  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<EOF
{
  "data-root": "${DOCKER_DATA}",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF

  # ---------- relocate containerd root to NVMe ----------
  # Handles two cases:
  #   1. config.toml has an existing `root = "..."` line → replace it
  #   2. config.toml has no `root` line → insert one at the top
  # (The default config from `containerd config default` sometimes omits it.)
  log "Relocating containerd root to NVMe ..."
  mkdir -p /etc/containerd
  if [[ ! -f /etc/containerd/config.toml ]]; then
    containerd config default > /etc/containerd/config.toml
  fi

  if grep -qE '^\s*root\s*=' /etc/containerd/config.toml; then
    sed -i -E "s#^\s*root\s*=.*#root = \"${CONTAINERD_DATA}\"#" /etc/containerd/config.toml
    log "containerd root replaced → ${CONTAINERD_DATA}"
  else
    sed -i "1i root = \"${CONTAINERD_DATA}\"" /etc/containerd/config.toml
    log "containerd root inserted → ${CONTAINERD_DATA}"
  fi

  # ---------- bring services back up ----------
  systemctl start containerd docker
  systemctl enable containerd docker

  # ---------- verify both relocations ----------
  local docker_root
  docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "")
  if [[ "$docker_root" == "$DOCKER_DATA" ]]; then
    log "Docker data-root verified → ${DOCKER_DATA}"
  else
    warn "Docker data-root is '${docker_root}' (expected ${DOCKER_DATA})"
  fi

  local containerd_root
  containerd_root=$(grep -E '^\s*root\s*=' /etc/containerd/config.toml 2>/dev/null | head -1 | sed 's/.*= *//; s/"//g')
  if [[ "$containerd_root" == "$CONTAINERD_DATA" ]]; then
    log "containerd root verified → ${CONTAINERD_DATA}"
  else
    warn "containerd root is '${containerd_root}' (expected ${CONTAINERD_DATA})"
  fi
}
