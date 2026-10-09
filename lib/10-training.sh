#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    lib/10-training.sh  
# Purpose: Set up the training directory, install the deploy helper (root-owned  
#          in /usr/local/sbin), wire the sudoers rule for the Web UI, and copy  
#          the training assets from the repo to the NVMe. The Web UI's  
#          Training tab drives the rest.  
# =============================================================================  
  
# Defaults if not already defined by the caller  
export TRAINING_DIR="${TRAINING_DIR:-${DATA_MOUNT}/training}"  
  
# Root-owned install location for the privileged helper (H2). The Web UI  
# user can sudo it but cannot rewrite it.  
DEPLOY_HELPER="/usr/local/sbin/dizercore-training-deploy"  
  
install_training() {  
  log "Setting up training infrastructure ..."  
  
  # ---------- training dir on NVMe ----------  
  mkdir -p "$TRAINING_DIR"  
  chown "${REAL_USER}:${REAL_USER}" "$TRAINING_DIR"  
  
  # ---------- copy training assets to web-ui/ ----------  
  local src_training="${REPO_ROOT}/training"  
  local dst_training="${WEB_UI_DIR}/training"  
  
  if [[ -d "$src_training" ]]; then  
    mkdir -p "$dst_training"  
    cp -r "$src_training/." "$dst_training/"  
    chown -R "${REAL_USER}:${REAL_USER}" "$dst_training"  
    log "Training assets copied to ${dst_training}"  
  else  
    warn "No training/ directory in repo — Training tab will be limited"  
  fi  
  
  # ---------- copy dataset-builder.py to TRAINING_DIR ----------  
  # training.py looks for it at <TRAINING_DIR>/dataset-builder.py  
  if [[ -f "${dst_training}/dataset-builder.py" ]]; then  
    cp "${dst_training}/dataset-builder.py" "${TRAINING_DIR}/dataset-builder.py"  
    chown "${REAL_USER}:${REAL_USER}" "${TRAINING_DIR}/dataset-builder.py"  
    log "Dataset builder installed at ${TRAINING_DIR}/dataset-builder.py"  
  fi  
  
  # ---------- install deploy helper to root-owned location ----------  
  if [[ -f "${WEB_UI_DIR}/training-deploy.sh" ]]; then  
    install -o root -g root -m 0755 \  
      "${WEB_UI_DIR}/training-deploy.sh" "$DEPLOY_HELPER"  
    log "Deploy helper installed at ${DEPLOY_HELPER} (root-owned)"  
    # Remove the user-writable copy so nothing can sudo the wrong script  
    rm -f "${WEB_UI_DIR}/training-deploy.sh"  
  else  
    warn "training-deploy.sh not found — Web UI training deploy will fail"  
  fi  
  
  # ---------- sudoers rules ----------  
  # printf instead of heredoc: trailing whitespace cannot break it  
  local sudoers_file="/etc/sudoers.d/dizercore-update"  
  printf '%s ALL=(ALL) NOPASSWD: /bin/bash %s/install.sh\n%s ALL=(ALL) NOPASSWD: %s/install.sh\n%s ALL=(ALL) NOPASSWD: %s\n' \  
    "$REAL_USER" "$SRC_DIR" "$REAL_USER" "$SRC_DIR" "$REAL_USER" "$DEPLOY_HELPER" \  
    > "$sudoers_file"  
  chmod 440 "$sudoers_file"  
  
  # Validate sudoers syntax before moving on  
  if visudo -c -f "$sudoers_file" >/dev/null 2>&1; then  
    log "Sudoers rules updated"  
  else  
    err "Sudoers validation failed — check ${sudoers_file}"  
  fi  
  
  # ---------- dataset marker check ----------  
  if [[ -f "${TRAINING_DIR}/dataset-builder.py" ]]; then  
    info "Dataset builder installed. Open the Web UI's Training tab to build."  
  fi  
}
