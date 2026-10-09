#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    install.sh
# Purpose: One-shot installer entry point.
#
# Steps:
#   1. NVMe setup
#   2. Docker
#   3. Gitea
#   4. llama.cpp + model
#   5. llama-server (persistent model in RAM)
#   6. Web UI
#   7. System optimization + overclock
#   8. Reference repository
#   9. Training infrastructure
#  10. README
#
# Re-exec safety: if running from /data, copies itself to /tmp and re-execs
# so the uninstall + NVMe wipe can destroy /data without breaking the script.
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/vekzla/DizerCore-AI.Assistant.git"
BRANCH="main"

# ---------- locate the repo ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"

# ---------- re-exec from /tmp if running from /data ----------
if [[ "$SCRIPT_DIR" == /data/* && -z "${DIZERCORE_REEXEC:-}" ]]; then
  RUN_DIR="/tmp/dizercore-run-$$"
  echo "[+] Detected run from /data — copying installer to ${RUN_DIR} ..."
  mkdir -p "$RUN_DIR"
  cp -r "$SCRIPT_DIR/." "$RUN_DIR/"
  cd "$RUN_DIR"
  export DIZERCORE_REEXEC=1
  exec bash "$RUN_DIR/install.sh" "$@"
fi

# ---------- self-detach when launched by the Web UI updater ----------  
# Runs install.sh inside a detached systemd unit so it survives the  
# prompt-gateway restart that happens at step 6.  
if [[ "${DIZERCORE_UPDATE:-0}" == "1" && -z "${DIZERCORE_UPDATE_UNIT:-}" ]]; then    
  UPDATE_USER="${SUDO_USER:-$(stat -c %U /data/dizercore-src 2>/dev/null || echo pi)}"  
  exec systemd-run --unit=dizercore-update --description="DizerCore self-update" --collect /bin/bash -c "cd /data/dizercore-src && git config --system --add safe.directory /data/dizercore-src && sudo -u ${UPDATE_USER} git fetch origin main && sudo -u ${UPDATE_USER} git reset --hard origin/main && DIZERCORE_UPDATE_UNIT=1 DIZERCORE_NON_INTERACTIVE=1 bash install.sh >> /var/log/dizercore-update.log 2>&1"    
fi

# ---------- bootstrap clone if running from a pipe ----------
if [[ -f "${SCRIPT_DIR}/lib/common.sh" ]]; then
  cd "$SCRIPT_DIR"
else
  BOOTSTRAP_DIR="/tmp/pi-prompt-gateway-$$"
  echo "[+] Cloning ${REPO_URL} to ${BOOTSTRAP_DIR} ..."
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq git >/dev/null 2>&1
  git clone -b "$BRANCH" "$REPO_URL" "$BOOTSTRAP_DIR"
  cd "$BOOTSTRAP_DIR"
  export DIZERCORE_BOOTSTRAP_DIR="$BOOTSTRAP_DIR"
fi

# ---------- root check ----------
if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh"
  exit 1
fi

# ---------- sanitize source files ----------  
find "$PWD" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.service' -o -name '*.json' \) -exec sed -i '1s/^\xEF\xBB\xBF//; s/\r$//; s/[ \t]*$//' {} +

# ---------- source common.sh (logging + paths) ----------
source lib/common.sh

# ---------- make the log accessible to the service user ----------
chown "${REAL_USER}:${REAL_USER}" "$LOG_FILE" 2>/dev/null || true
chmod 644 "$LOG_FILE" 2>/dev/null || true

# ---------- register exit trap for the report ----------
trap print_report EXIT

# ---------- open the log session ----------
{
  echo ""
  echo "==============================================================="
  echo "  DizerCore install started: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "  User:       ${SUDO_USER:-$(whoami)}"
  echo "  PID:        $$"
  echo "  Root FS:    $(df -P /var/log | tail -1 | awk '{print $1}')"
  echo "  Source:     ${PWD}"
  echo "==============================================================="
} >> "$LOG_FILE"

echo "[i] Logging to ${LOG_FILE} (on SD card, survives /data wipes)"

# ---------- source the rest of the modules ----------
source lib/01-nvme.sh
source lib/02-docker.sh
source lib/03-gitea.sh
source lib/04-llama.sh
source lib/05-webui.sh
source lib/06-optimize.sh
source lib/07-readme.sh
source lib/08-reference-repo.sh
source lib/09-llama-server.sh
source lib/11-training.sh
source "${PWD}/uninstall.sh"  # provides perform_uninstall_installer() for the NVMe wipe step

# ---------- warn if not in tmux and stdin is piped ----------
if [[ -z "${TMUX:-}" && ! -t 0 ]]; then
  warn "This installer has interactive prompts (step 8 asks for a repo URL)."
  warn "Running outside tmux with a piped stdin will skip those prompts."
  warn "Recommended: run bootstrap.sh instead —"
  warn "  curl -fsSL https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/main/bootstrap.sh -o /tmp/dca.sh && sudo bash /tmp/dca.sh"
  warn "Continuing in 5 seconds. Press Ctrl+C to abort."
  sleep 5
fi

# ---------- run steps ----------
step "1/10: NVMe setup"
install_nvme

step "2/10: Docker"
install_docker

step "3/10: Gitea"
install_gitea

step "4/10: llama.cpp + model"
install_llama

step "5/10: llama-server (persistent model in RAM)"
install_llama_server

step "6/10: Web UI"
install_webui

step "7/10: System optimization + overclock"
install_optimize

step "8/10: Reference repository"
install_reference_repo

step "9/10: Training infrastructure"
install_training

step "10/10: README"
install_readme

# ---------- summary ----------
print_summary

# ---------- cleanup ----------
if [[ -n "${DIZERCORE_BOOTSTRAP_DIR:-}" && -d "$DIZERCORE_BOOTSTRAP_DIR" ]]; then
  rm -rf "$DIZERCORE_BOOTSTRAP_DIR"
fi
if [[ -n "${DIZERCORE_REEXEC:-}" && -d "/tmp/dizercore-run-$$" ]]; then
  rm -rf "/tmp/dizercore-run-$$" 2>/dev/null || true
fi
