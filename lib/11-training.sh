#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    lib/11-training.sh  
# Purpose: Set up the training directory, install the deploy helper, wire  
#          the sudoers rule for the Web UI, and copy the training assets  
#          from the repo to the NVMe. The Web UI's Training tab drives the  
#          rest.  
# =============================================================================  
  
# Defaults if not already defined by the caller  
export TRAINING_DIR="${TRAINING_DIR:-${DATA_MOUNT}/training}"  
  
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
  
  # ---------- make deploy helper executable ----------  
  if [[ -f "${WEB_UI_DIR}/training-deploy.sh" ]]; then  
    chmod +x "${WEB_UI_DIR}/training-deploy.sh"  
    chown root:root "${WEB_UI_DIR}/training-deploy.sh"  
    log "Deploy helper ready at ${WEB_UI_DIR}/training-deploy.sh"  
  else  
    warn "training-deploy.sh not found — Web UI training deploy will fail"  
  fi  
  
  # ---------- sudoers rule for the deploy helper ----------  
  local sudoers_file="/etc/sudoers.d/dizercore-update"  
  cat > "$sudoers_file" <<EOF  
${REAL_USER} ALL=(ALL) NOPASSWD: /bin/bash ${SRC_DIR}/install.sh  
${REAL_USER} ALL=(ALL) NOPASSWD: ${SRC_DIR}/install.sh  
${REAL_USER} ALL=(ALL) NOPASSWD: ${WEB_UI_DIR}/training-deploy.sh  
EOF  
  chmod 440 "$sudoers_file"  
  
  if visudo -c -f "$sudoers_file" >/dev/null 2>&1; then  
    log "Sudoers rules updated"  
  else  
    err "Sudoers validation failed — check ${sudoers_file}"  
  fi  
  
  if [[ -f "${TRAINING_DIR}/dataset-builder.py" ]]; then  
    info "Dataset builder installed. Open the Web UI's Training tab to build."  
  fi  
}
