#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    uninstall.sh  
# Purpose: Completely remove DizerCore and everything the installer added.  
#  
# Modes:  
#   Standalone (default)  — full cleanup, prompts for confirmation  
#   --yes                 — non-interactive  
#   --keep-nvme           — preserve /data and its contents  
#   --keep-docker         — leave Docker Engine installed  
#   --installer-mode      — called from install.sh; only stops services,  
#                           removes sudoers, and cleans containers. Skips  
#                           apt/data/temp/NVMe cleanup because the parent  
#                           installer owns those steps. Critical: does NOT  
#                           delete /tmp/dizercore-* or /tmp/pi-prompt-*,  
#                           since the running installer's CWD may live there.  
# =============================================================================  
set -euo pipefail  
  
# ---------- arg parsing (only when run directly) ----------  
ASSUME_YES=0  
KEEP_NVME=0  
KEEP_DOCKER=0  
INSTALLER_MODE=0  
  
_parse_args() {  
  for arg in "$@"; do  
    case "$arg" in  
      --yes|-y)         ASSUME_YES=1 ;;  
      --keep-nvme)      KEEP_NVME=1 ;;  
      --keep-docker)    KEEP_DOCKER=1 ;;  
      --installer-mode) INSTALLER_MODE=1; ASSUME_YES=1; KEEP_NVME=1 ;;  
      --help|-h)  
        cat <<EOF  
DizerCore uninstaller  
  
  --yes, -y         Non-interactive. Assume "yes" for every prompt.  
  --keep-nvme       Preserve the NVMe partition and /data contents.  
  --keep-docker     Leave Docker installed.  
  --installer-mode  Called from install.sh — only stops services, removes  
                    sudoers, and cleans containers. Parent installer owns  
                    everything else.  
  --help, -h        Show this message.  
EOF  
        exit 0  
        ;;  
      *) echo "Unknown option: $arg"; exit 1 ;;  
    esac  
  done  
}  
  
# ---------- colors + logging ----------  
if declare -f log >/dev/null 2>&1; then  
  ulog()  { log  "$@"; }  
  uwarn() { warn "$@"; }  
  uinfo() { info "$@"; }  
  ustep() { step "$@"; }  
else  
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'  
  BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'  
  ulog()  { echo -e "${GREEN}[+]${NC} $*"; }  
  uwarn() { echo -e "${YELLOW}[!]${NC} $*" >&2; }  
  uinfo() { echo -e "${BLUE}[i]${NC} $*"; }  
  ustep() { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }  
fi  
  
# ---------- config ----------  
UNINSTALL_NVME_DEV="/dev/nvme0n1"  
UNINSTALL_DATA_MOUNT="/data"  
UNINSTALL_LOG_FILE="/var/log/dizercore-uninstall.log"  
  
# =============================================================================  
# Installer mode — minimal cleanup that doesn't touch anything the parent  
# installer still needs.  
# =============================================================================  
perform_uninstall_installer() {  
  ustep "U1/3: Stopping DizerCore services"  
  # L12: stop every DizerCore service, not just prompt-gateway  
  for svc in prompt-gateway index-watcher llama-server; do  
    systemctl stop "$svc" 2>/dev/null && ulog "Stopped ${svc}" || uinfo "${svc} not running"  
    systemctl disable "$svc" 2>/dev/null || true  
    rm -f "/etc/systemd/system/${svc}.service"  
  done  
  rm -f /etc/systemd/system/dizercore-update.service  
  rm -f /etc/systemd/system/dizercore-update.timer  
  rm -f /etc/systemd/system/dizercore-update.path  
  rm -f /usr/local/sbin/dizercore-training-deploy  
  pkill -f "llama-cli" 2>/dev/null || true  
  pkill -f "index-watcher.py" 2>/dev/null || true  
  systemctl daemon-reload  
  ulog "Service units removed"  
  
  ustep "U2/3: Removing sudoers rules"  
  if [[ -f /etc/sudoers.d/dizercore-update ]]; then  
    rm -f /etc/sudoers.d/dizercore-update  
    ulog "Removed /etc/sudoers.d/dizercore-update"  
  else  
    uinfo "No sudoers rules found"  
  fi  
  
  ustep "U3/3: Removing containers, volumes, networks"  
  if command -v docker &>/dev/null; then  
    docker ps -q 2>/dev/null | xargs -r docker stop 2>/dev/null || true  
    docker ps -aq 2>/dev/null | xargs -r docker rm 2>/dev/null || true  
    docker volume ls -q 2>/dev/null | xargs -r docker volume rm 2>/dev/null || true  
    docker network ls --format '{{.Name}}' 2>/dev/null \  
      | grep -vE '^(bridge|host|none)$' \  
      | xargs -r docker network rm 2>/dev/null || true  
    ulog "Containers, volumes, and networks removed"  
  else  
    uinfo "Docker not installed — skipping"  
  fi  
  
  ulog "Installer-mode cleanup complete"  
}  
  
# =============================================================================  
# Standalone mode — full uninstall.  
# =============================================================================  
perform_uninstall_standalone() {  
  # ---------- 1. stop services ----------  
  ustep "1/9: Stopping DizerCore services"  
  # L12: stop every DizerCore service, not just prompt-gateway  
  for svc in prompt-gateway index-watcher llama-server; do  
    systemctl stop "$svc" 2>/dev/null && ulog "Stopped ${svc}" || uinfo "${svc} not running"  
    systemctl disable "$svc" 2>/dev/null || true  
    rm -f "/etc/systemd/system/${svc}.service"  
  done  
  rm -f /etc/systemd/system/dizercore-update.service  
  rm -f /etc/systemd/system/dizercore-update.timer  
  rm -f /etc/systemd/system/dizercore-update.path  
  rm -f /usr/local/sbin/dizercore-training-deploy  
  pkill -f "dizercore-src/install.sh" 2>/dev/null || true  
  pkill -f "llama-cli" 2>/dev/null || true  
  pkill -f "prompt-gateway" 2>/dev/null || true  
  pkill -f "index-watcher.py" 2>/dev/null || true  
  systemctl daemon-reload  
  ulog "Service units removed"  
  
  # ---------- 2. remove sudoers ----------  
  ustep "2/9: Removing sudoers rules"  
  if [[ -f /etc/sudoers.d/dizercore-update ]]; then  
    rm -f /etc/sudoers.d/dizercore-update  
    ulog "Removed /etc/sudoers.d/dizercore-update"  
  else  
    uinfo "No sudoers rules found"  
  fi  
  
  # ---------- 3. docker containers ----------  
  ustep "3/9: Removing Docker containers and volumes"  
  if command -v docker &>/dev/null; then  
    docker ps -q 2>/dev/null | xargs -r docker stop 2>/dev/null || true  
    docker ps -aq 2>/dev/null | xargs -r docker rm 2>/dev/null || true  
    docker volume ls -q 2>/dev/null | xargs -r docker volume rm 2>/dev/null || true  
    if [[ $KEEP_DOCKER -eq 0 ]]; then  
      docker network ls --format '{{.Name}}' 2>/dev/null \  
        | grep -vE '^(bridge|host|none)$' \  
        | xargs -r docker network rm 2>/dev/null || true  
    fi  
    ulog "Docker containers removed"  
  else  
    uinfo "Docker not installed — skipping"  
  fi  
  
  # ---------- 4. /data ----------  
  ustep "4/9: Unmounting /data"  
  systemctl stop docker docker.socket containerd 2>/dev/null || true  
  
  if mountpoint -q "$UNINSTALL_DATA_MOUNT"; then  
    uinfo "Unmounting ${UNINSTALL_DATA_MOUNT} ..."  
    umount -f "$UNINSTALL_DATA_MOUNT" 2>/dev/null \  
      || umount -l "$UNINSTALL_DATA_MOUNT" 2>/dev/null || true  
    if mountpoint -q "$UNINSTALL_DATA_MOUNT"; then  
      uwarn "Could not unmount ${UNINSTALL_DATA_MOUNT} — a process is still using it"  
    else  
      ulog "Unmounted ${UNINSTALL_DATA_MOUNT}"  
    fi  
  else  
    uinfo "${UNINSTALL_DATA_MOUNT} not mounted"  
  fi  
  
  # M9: never wipe or delete a still-mounted filesystem  
  if [[ $KEEP_NVME -eq 0 ]] && mountpoint -q "$UNINSTALL_DATA_MOUNT"; then  
    uwarn "${UNINSTALL_DATA_MOUNT} is still mounted — refusing to wipe/delete."  
    uwarn "Find the holder with: lsof +D ${UNINSTALL_DATA_MOUNT}"  
    uwarn "Re-run with --keep-nvme to skip /data, or free the mount and retry."  
    exit 1  
  fi  
  
  if grep -q "$UNINSTALL_DATA_MOUNT" /etc/fstab 2>/dev/null; then  
    cp /etc/fstab "/etc/fstab.dizercore-backup.$(date +%s)"  
    sed -i "\#${UNINSTALL_DATA_MOUNT}#d" /etc/fstab  
    systemctl daemon-reload  
    ulog "Removed /data entry from /etc/fstab (backup saved)"  
  else  
    uinfo "No /data entry in /etc/fstab"  
  fi  
  
  # ---------- 5. wipe NVMe ----------  
  ustep "5/9: Wiping NVMe"  
  if [[ $KEEP_NVME -eq 1 ]]; then  
    uinfo "--keep-nvme specified — preserving NVMe partition"  
  elif [[ -b "$UNINSTALL_NVME_DEV" ]]; then  
    uinfo "Wiping filesystem signatures on ${UNINSTALL_NVME_DEV} ..."  
    wipefs -a "${UNINSTALL_NVME_DEV}p1" 2>/dev/null || true  
    wipefs -a "${UNINSTALL_NVME_DEV}" 2>/dev/null || true  
    ulog "NVMe signatures wiped (partition table intact)"  
  else  
    uinfo "NVMe device not found — skipping"  
  fi  
  
  # ---------- 6. /data directory ----------  
  ustep "6/9: Removing DizerCore data directories"  
  if [[ $KEEP_NVME -eq 1 ]]; then  
    uinfo "--keep-nvme — preserving /data"  
  elif [[ -d "$UNINSTALL_DATA_MOUNT" ]]; then  
    # mountpoint already verified clear by the guard above  
    rm -rf "$UNINSTALL_DATA_MOUNT"  
    ulog "Removed ${UNINSTALL_DATA_MOUNT}"  
  else  
    uinfo "${UNINSTALL_DATA_MOUNT} not present"  
  fi  
  
  # ---------- 7. apt packages ----------  
  ustep "7/9: Uninstalling apt packages"  
  local pkgs=(zram-tools jq tmux cmake build-essential)  
  if [[ $KEEP_DOCKER -eq 0 ]]; then  
    pkgs+=(  
      docker-ce docker-ce-cli containerd.io  
      docker-buildx-plugin docker-compose-plugin  
    )  
  fi  
  for pkg in "${pkgs[@]}"; do  
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then  
      uinfo "Removing ${pkg} ..."  
      DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq "$pkg" 2>/dev/null \  
        || uwarn "Failed to remove ${pkg}"  
    else  
      uinfo "${pkg} not installed"  
    fi  
  done  
  DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -qq 2>/dev/null || true  
  DEBIAN_FRONTEND=noninteractive apt-get autoclean -qq 2>/dev/null || true  
  ulog "apt packages removed and autoremoved"  
  
  # ---------- 8. docker repo, configs, zram ----------  
  ustep "8/9: Removing Docker repository, configs, ZRAM tuning"  
  
  if [[ $KEEP_DOCKER -eq 0 ]]; then  
    rm -f /etc/apt/sources.list.d/docker.list  
    rm -f /etc/apt/keyrings/docker.asc  
    rmdir /etc/apt/keyrings 2>/dev/null || true  
    rm -f /etc/docker/daemon.json  
    rmdir /etc/docker 2>/dev/null || true  
    rm -f /etc/containerd/config.toml  
    rmdir /etc/containerd 2>/dev/null || true  
    if getent group docker &>/dev/null; then  
      [[ -z "$(getent group docker | cut -d: -f4)" ]] \  
        && groupdel docker 2>/dev/null || true  
    fi  
    rm -rf /var/lib/docker /var/lib/containerd 2>/dev/null || true  
    ulog "Docker repo and configs removed"  
  else  
    uinfo "Docker repo and configs preserved"  
  fi  
  
  rm -f /etc/default/zramswap 2>/dev/null || true  
  if grep -q "^vm.swappiness" /etc/sysctl.conf 2>/dev/null; then  
    cp /etc/sysctl.conf "/etc/sysctl.conf.dizercore-backup.$(date +%s)"  
    sed -i '/^vm\.swappiness/d' /etc/sysctl.conf  
    sysctl -p 2>/dev/null || true  
    ulog "Reverted vm.swappiness (backup saved)"  
  fi  
  rm -f /etc/logrotate.d/dizercore 2>/dev/null || true  
  
  # ---------- 9. temp + user configs ----------  
  ustep "9/9: Cleaning temp files and user configs"  
  rm -rf /tmp/pi-prompt-gateway-* 2>/dev/null || true  
  rm -rf /tmp/dizercore-* 2>/dev/null || true  
  rm -f /tmp/dca.sh /tmp/bs.sh /tmp/dca-install.sh /tmp/dca-uninstall.sh 2>/dev/null || true  
  rm -f /tmp/test-model.gguf 2>/dev/null || true  
  
  local real_user="${SUDO_USER:-pi}"  
  if id -nG "$real_user" 2>/dev/null | grep -qw docker; then  
    gpasswd -d "$real_user" docker 2>/dev/null || true  
  fi  
  
  ulog "Temp files and user configs cleaned"  
}  
  
# =============================================================================  
# Public entry point — dispatches based on mode.  
# =============================================================================  
perform_uninstall() {  
  local mode="${1:-standalone}"  
  if [[ "$mode" == "installer" || "$INSTALLER_MODE" -eq 1 ]]; then  
    perform_uninstall_installer  
  else  
    perform_uninstall_standalone  
  fi  
}  
  
# =============================================================================  
# Standalone entry point — argument parsing, logging, confirmation.  
# =============================================================================  
_uninstall_standalone() {  
  _parse_args "$@"  
  
  if [[ $EUID -ne 0 ]]; then  
    echo "Run as root: sudo bash uninstall.sh"  
    exit 1  
  fi  
  
  # ---------- logging (standalone only) ----------  
  mkdir -p "$(dirname "$UNINSTALL_LOG_FILE")"  
  {  
    echo ""  
    echo "==============================================================="  
    echo "  DizerCore uninstall started: $(date '+%Y-%m-%d %H:%M:%S')"  
    echo "  User: ${SUDO_USER:-pi}"  
    echo "==============================================================="  
  } >> "$UNINSTALL_LOG_FILE"  
  exec > >(tee -a "$UNINSTALL_LOG_FILE") 2>&1  
  
  echo ""  
  echo -e "\033[1m\033[0;31m╔═══════════════════════════════════════════════════════════════════╗\033[0m"  
  echo -e "\033[1m\033[0;31m║                    DizerCore Uninstaller                          ║\033[0m"  
  echo -e "\033[1m\033[0;31m╚═══════════════════════════════════════════════════════════════════╝\033[0m"  
  echo ""  
  echo "  This will remove:"  
  echo "    • DizerCore Web UI service and files"  
  echo "    • Gitea and PostgreSQL containers"  
  echo "    • llama.cpp, models, prompt history"  
  echo "    • Reference repositories"  
  [[ $KEEP_DOCKER -eq 0 ]] && echo "    • Docker Engine and all images"  
  [[ $KEEP_DOCKER -eq 0 ]] && echo "    • Installed apt packages (cmake, tmux, jq, zram-tools, etc.)"  
  [[ $KEEP_NVME -eq 0 ]] && echo -e "    • \033[1m\033[0;31m/data contents — NVMe will be wiped\033[0m"  
  echo ""  
  echo -e "  \033[1mLog: ${UNINSTALL_LOG_FILE}\033[0m"  
  echo ""  
  
  if [[ $ASSUME_YES -eq 0 ]]; then  
    read -rp "  Proceed with uninstall? [y/N]: " response < /dev/tty  
    if [[ "${response,,}" != "y" && "${response,,}" != "yes" ]]; then  
      echo "  Aborted."  
      exit 0  
    fi  
  fi  
  
  perform_uninstall_standalone  
  
  echo ""  
  echo -e "\033[0;36m═══════════════════════════════════════════════════════════════════\033[0m"  
  echo -e "\033[0;36m  Uninstall Complete\033[0m"  
  echo -e "\033[0;36m═══════════════════════════════════════════════════════════════════\033[0m"  
  echo ""  
  echo -e "  \033[2mFull log: ${UNINSTALL_LOG_FILE}\033[0m"  
  echo ""  
  echo -e "  \033[1mRecommended: reboot\033[0m to complete the cleanup."  
  echo ""  
}  
  
# Only run when executed directly, not when sourced.  
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then  
  _uninstall_standalone "$@"  
fi
