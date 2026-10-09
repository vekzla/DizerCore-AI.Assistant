#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    web-ui/training-deploy.sh  
# Installed: /usr/local/sbin/dizercore-training-deploy  (root:root, 0755)  
# Purpose: Privileged helper for switching llama-server's active model.  
#          Called by the Web UI via sudo with NOPASSWD.  
#  
# Usage:  
#   dizercore-training-deploy deploy <model_path>  
#   dizercore-training-deploy revert  
# =============================================================================  
set -euo pipefail  
  
SERVICE="/etc/systemd/system/llama-server.service"  
BACKUP="${SERVICE}.pre-training"  
  
# Accept only real .gguf files under the models dir. No spaces, no shell  
# metacharacters — this is what makes the sed below safe (H4).  
ALLOWED_PATH_RE='^/data/models/[A-Za-z0-9._-]+\.gguf$'  
  
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
    if [[ ! "$model_path" =~ $ALLOWED_PATH_RE ]]; then  
      echo "Refused: model path must match ${ALLOWED_PATH_RE}" >&2  
      exit 1  
    fi  
    if [[ ! -f "$model_path" ]]; then  
      echo "Model not found: $model_path" >&2  
      exit 1  
    fi  
    # GGUF magic check — same gate the upload endpoint applies  
    if ! head -c 4 "$model_path" | grep -q 'GGUF'; then  
      echo "Refused: $model_path is not a GGUF file" >&2  
      exit 1  
    fi  
  
    # Keep a one-time backup of the pre-training unit for `revert`  
    if [[ ! -f "$BACKUP" ]]; then  
      cp "$SERVICE" "$BACKUP"  
    fi  
  
    # The path passed the whitelist regex above, so it contains none of  
    # sed's metacharacters (| & \) — safe to interpolate.  
    sed -i "s| -m [^ ]*| -m ${model_path}|" "$SERVICE"  
  
    systemctl daemon-reload  
    systemctl restart llama-server.service  
    echo "Deployed model: $model_path"  
    ;;  
  
  revert)  
    if [[ ! -f "$BACKUP" ]]; then  
      echo "No backup found at $BACKUP — nothing to revert" >&2  
      exit 1  
    fi  
    cp "$BACKUP" "$SERVICE"  
    systemctl daemon-reload  
    systemctl restart llama-server.service  
    echo "Reverted to pre-training unit"  
    ;;  
  
  *)  
    echo "Usage: $0 {deploy <model_path>|revert}" >&2  
    exit 1  
    ;;  
esac
