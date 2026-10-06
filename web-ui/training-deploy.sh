#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    web-ui/training-deploy.sh
# Purpose: Privileged helper for switching llama-server's active model.
#          Called by the Web UI via sudo with NOPASSWD.
#
# Usage:
#   training-deploy.sh deploy <model_path>
#   training-deploy.sh revert
# =============================================================================
set -euo pipefail

SERVICE="/etc/systemd/system/llama-server.service"
BACKUP="${SERVICE}.pre-training"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (via sudo)" >&2
  exit 1
fi

action="${1:-}"

case "$action" in
  deploy)
    model_path="${2:-}"
    if [[ -z "$model_path" ]]; then
      echo "Usage: $0 deploy <model_path>" >&2
      exit 1
    fi
    if [[ ! -f "$model_path" ]]; then
      echo "Model not found: $model_path" >&2
      exit 1
    fi

    # Backup the current unit only if we don't already have one
    if [[ ! -f "$BACKUP" ]]; then
      cp "$SERVICE" "$BACKUP"
      echo "Backed up current unit → $BACKUP"
    fi

    # Replace the -m argument in the ExecStart line
    sed -i "s| -m [^ ]*| -m ${model_path}|" "$SERVICE"

    systemctl daemon-reload
    systemctl restart llama-server
    echo "Deployed: $model_path"
    ;;

  revert)
    if [[ ! -f "$BACKUP" ]]; then
      echo "No backup found at $BACKUP — nothing to revert to" >&2
      exit 1
    fi
    mv "$BACKUP" "$SERVICE"
    systemctl daemon-reload
    systemctl restart llama-server
    echo "Reverted to base model"
    ;;

  *)
    echo "Usage: $0 {deploy <model>|revert}" >&2
    exit 1
    ;;
esac
