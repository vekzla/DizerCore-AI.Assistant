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
    if [[ "$(head -c 4 "$model_path")" != "GGUF" ]]; then  
      echo "Refused: $model_path is not a GGUF file" >&2  
      exit 1  
    fi  
  
    # Backup the current unit only if we don't already have one  
    if [[ ! -f "$BACKUP" ]]; then  
      cp "$SERVICE" "$BACKUP"  
      echo "Backed up current unit → $BACKUP"  
    fi  
  
    # Replace the -m argument in the ExecStart line.  
    # Path is already whitelist-validated, but escape the sed  
    # replacement anyway as defence in depth.  
    model_path_esc="${model_path//\\/\\\\}"  
    model_path_esc="${model_path_esc//|/\\|}"  
    model_path_esc="${model_path_esc//&/\\&}"  
    sed -i "s| -m [^ ]*| -m ${model_path_esc}|" "$SERVICE"  
  
    systemctl daemon-reload  
    systemctl restart llama-server  
    echo "Deployed: $model_path"  
    ;;  
  
  revert)  
    if [[ ! -f "$BACKUP" ]]; then  
      echo "No backup found at $BACKUP — nothing to revert to" >&2  
      exit 1  
    fi  
    # cp (not mv) so the backup survives and revert can run again  
    cp "$BACKUP" "$SERVICE"  
    systemctl daemon-reload  
    systemctl restart llama-server  
    echo "Reverted to base model"  
    ;;  
  
  *)  
    echo "Usage: $0 {deploy <model>|revert}" >&2  
    exit 1  
    ;;  
esac
