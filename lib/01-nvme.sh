#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    lib/01-nvme.sh  
# Purpose: Detect prior installs, prompt the user, run the full uninstaller  
#          if requested, then mount/format/write the NVMe and persist the  
#          source repo.  
# =============================================================================  
  
# Ask a y/N question directly on /dev/tty. Prints the prompt, reads the answer,  
# and returns 0 only on explicit y/yes. Does not depend on confirm().  
nvme_ask_yes_no() {  
  local prompt="$1" response=""  
  printf "%b%s%b [y/N]: " "$YELLOW" "$prompt" "$NC" > /dev/tty 2>/dev/null || printf "%s [y/N]: " "$prompt"  
  if ! read -r response < /dev/tty 2>/dev/null; then  
    read -r response || response=""  
  fi  
  case "${response,,}" in  
    y|yes) return 0 ;;  
    *)     return 1 ;;  
  esac  
}  
  
install_nvme() {  
  log "Checking NVMe device ${NVME_DEV} ..."  
  
  if [[ ! -b "$NVME_DEV" ]]; then  
    err "NVMe device ${NVME_DEV} not found. Is the M.2 HAT connected?"  
  fi  
  
  local prior_install=false  
  if [[ -f "${DATA_MOUNT}/.dizercore-installed" ]] || \  
     { [[ -d "${DATA_MOUNT}/gitea" && -d "${DATA_MOUNT}/prompt-history" ]]; }; then  
    prior_install=true  
  fi  
  
  if [[ "$prior_install" == "true" ]]; then  
    if [[ "$DIZERCORE_NON_INTERACTIVE" == "1" ]]; then  
      info "Non-interactive mode — reusing existing installation"  
      if ! mountpoint -q "$DATA_MOUNT"; then  
        mount -a 2>/dev/null || mount "${NVME_DEV}p1" "$DATA_MOUNT"  
      fi  
    else  
      info "Prior DizerCore installation detected on ${DATA_MOUNT}"  
      echo ""  
      echo -e "  ${BOLD}Existing data found:${NC}"  
      [[ -d "${DATA_MOUNT}/gitea" ]] && echo -e "    ${DIM}•${NC} Gitea repos and config"  
      [[ -d "${DATA_MOUNT}/postgres" ]] && echo -e "    ${DIM}•${NC} PostgreSQL database"  
      if [[ -f "${DATA_MOUNT}/prompt-history/history.json" ]]; then  
        local count  
        count=$(jq 'length' "${DATA_MOUNT}/prompt-history/history.json" 2>/dev/null || echo '?')  
        echo -e "    ${DIM}•${NC} Prompt history (${count} entries)"  
      fi  
      [[ -d "${MODELS_DIR}" ]] && echo -e "    ${DIM}•${NC} Downloaded models ($(du -sh "${MODELS_DIR}" 2>/dev/null | cut -f1))"  
      echo ""  
      echo -e "  ${BOLD}${YELLOW}WARNING:${NC} A fresh install will PERMANENTLY DELETE all data above."  
      echo -e "  ${BOLD}${YELLOW}WARNING:${NC} Docker, Gitea, llama.cpp, and all their configs will also be removed."  
      echo ""  
  
      if nvme_ask_yes_no "Wipe everything and start fresh (runs full uninstall)?"; then  
        log "User chose FRESH INSTALL — running full uninstall"  
        perform_uninstall "installer"  
        log "Uninstall complete — proceeding with NVMe wipe"  
        wipe_nvme  
      else  
        log "User chose REUSE — keeping existing data"  
        if ! mountpoint -q "$DATA_MOUNT"; then  
          log "Mounting existing ${DATA_MOUNT} ..."  
          mount -a 2>/dev/null || mount "${NVME_DEV}p1" "$DATA_MOUNT"  
        fi  
        info "Existing installation preserved"  
      fi  
    fi  
  else  
    if mountpoint -q "$DATA_MOUNT"; then  
      info "${DATA_MOUNT} is already mounted — skipping format."  
    else  
      if [[ -b "${NVME_DEV}p1" ]]; then  
        NVME_PART="${NVME_DEV}p1"  
        info "Using existing partition ${NVME_PART}"  
      else  
        warn "No partition found on ${NVME_DEV} — creating one."  
        parted -s "$NVME_DEV" mklabel gpt  
        parted -s "$NVME_DEV" mkpart primary ext4 0% 100%  
        sleep 2  
        NVME_PART="${NVME_DEV}p1"  
        mkfs.ext4 -F -L pi-data "$NVME_PART"  
      fi  
  
      local NVME_UUID  
      NVME_UUID=$(blkid -s UUID -o value "$NVME_PART")  
      [[ -n "$NVME_UUID" ]] || err "Could not read UUID of ${NVME_PART}"  
  
      mkdir -p "$DATA_MOUNT"  
      if ! grep -q "$NVME_UUID" /etc/fstab 2>/dev/null; then  
        echo "UUID=${NVME_UUID} ${DATA_MOUNT} ext4 defaults,noatime 0 2" >> /etc/fstab  
      fi  
      systemctl daemon-reload  
      mount -a  
      mountpoint -q "$DATA_MOUNT" || err "Failed to mount ${DATA_MOUNT}"  
      log "NVMe mounted at ${DATA_MOUNT} (UUID=${NVME_UUID})"  
    fi  
  fi  
  
  mkdir -p "$GITEA_DIR" "$GITEA_DB_DIR" "$DOCKER_DATA" "$CONTAINERD_DATA" "$MODELS_DIR" "$PROMPT_HISTORY" "$WEB_UI_DIR" "$(dirname "$VENV_DIR")" "$LLAMA_DIR" "$REFERENCE_DIR" "$LOGO_DIR"  
  log "Directory structure ready"  
  
  if [[ ! -d "${SRC_DIR}/.git" ]]; then  
    log "Persisting source repo to ${SRC_DIR} ..."  
    if [[ -n "${DIZERCORE_BOOTSTRAP_DIR:-}" && -d "${DIZERCORE_BOOTSTRAP_DIR}/.git" ]]; then  
      cp -r "$DIZERCORE_BOOTSTRAP_DIR" "$SRC_DIR"  
    elif [[ -n "${DIZERCORE_REEXEC:-}" && -d "/tmp/dizercore-run-$$/.git" ]]; then  
      cp -r "/tmp/dizercore-run-$$" "$SRC_DIR"  
    else  
      git clone -b main https://github.com/vekzla/DizerCore-AI.Assistant.git "$SRC_DIR"  
    fi  
    chown -R "${REAL_USER}:${REAL_USER}" "$SRC_DIR"  
    log "Source repo persisted"  
  else  
    info "Source repo already present at ${SRC_DIR}"  
  fi  
  
  touch "${DATA_MOUNT}/.dizercore-installed"  
}  
  
wipe_nvme() {  
  log "Stopping services that may be using ${DATA_MOUNT} ..."  
  systemctl stop prompt-gateway 2>/dev/null || true  
  systemctl stop docker docker.socket containerd 2>/dev/null || true  
  docker ps -q 2>/dev/null | xargs -r docker stop 2>/dev/null || true  
  sleep 2  
  
  if mountpoint -q "$DATA_MOUNT"; then  
    log "Unmounting ${DATA_MOUNT} ..."  
    umount -f "$DATA_MOUNT" 2>/dev/null || umount -l "$DATA_MOUNT" 2>/dev/null || true  
  fi  
  
  if grep -q "$DATA_MOUNT" /etc/fstab; then  
    sed -i "\#${DATA_MOUNT}#d" /etc/fstab  
  fi  
  systemctl daemon-reload  
  
  log "Wiping filesystem signatures on ${NVME_DEV} ..."  
  wipefs -a "${NVME_DEV}" 2>/dev/null || true  
  wipefs -a "${NVME_DEV}p1" 2>/dev/null || true  
  sleep 1  
  
  log "Creating fresh GPT partition table ..."  
  parted -s "$NVME_DEV" mklabel gpt  
  parted -s "$NVME_DEV" mkpart primary ext4 0% 100%  
  sleep 2  
  
  log "Formatting ${NVME_DEV}p1 as ext4 ..."  
  mkfs.ext4 -F -L pi-data "${NVME_DEV}p1"  
  
  local NVME_UUID  
  NVME_UUID=$(blkid -s UUID -o value "${NVME_DEV}p1")  
  echo "UUID=${NVME_UUID} ${DATA_MOUNT} ext4 defaults,noatime 0 2" >> /etc/fstab  
  systemctl daemon-reload  
  
  mkdir -p "$DATA_MOUNT"  
  mount -a  
  mountpoint -q "$DATA_MOUNT" || err "Failed to remount ${DATA_MOUNT}"  
  log "NVMe wiped and remounted (UUID=${NVME_UUID})"  
  
  mkdir -p "$GITEA_DIR" "$GITEA_DB_DIR" "$DOCKER_DATA" "$CONTAINERD_DATA" "$MODELS_DIR" "$PROMPT_HISTORY" "$WEB_UI_DIR" "$(dirname "$VENV_DIR")" "$LLAMA_DIR" "$REFERENCE_DIR" "$LOGO_DIR"  
  
  systemctl start containerd docker 2>/dev/null || true  
  log "NVMe wipe complete"  
}
