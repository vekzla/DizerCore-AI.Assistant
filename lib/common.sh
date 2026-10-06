#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    lib/common.sh  
# Purpose: Shared constants, paths, colors, structured logging, model registry.  
# =============================================================================  
  
# ---------- log file ----------  
export LOG_FILE="${LOG_FILE:-/var/log/dizercore-install.log}"  
mkdir -p "$(dirname "$LOG_FILE")"  
touch "$LOG_FILE" 2>/dev/null || true  
chmod 666 "$LOG_FILE" 2>/dev/null || true   # readable by the web UI's log endpoint  
  
# ---------- counters ----------  
export INSTALLED_COUNT=0  
export SKIPPED_COUNT=0  
export WARN_COUNT=0  
export ERROR_COUNT=0  
  
_log_ts() { date '+%Y-%m-%d %H:%M:%S'; }  
  
_log_write() {  
  local tag="$1"; shift  
  local msg="$*"  
  msg=$(printf '%s' "$msg" | sed 's/\x1b\[[0-9;]*m//g')  
  printf '%s [%-7s] %s\n' "$(_log_ts)" "$tag" "$msg" >> "$LOG_FILE" 2>/dev/null || true  
}  
  
# ---------- terminal colors ----------  
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'  
BLUE='\033[0;34m'; CYAN='\033[0;36m'; MAGENTA='\033[0;35m'  
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'  
  
log()  { echo -e "${GREEN}[+]${NC} $*"; _log_write "INSTALL" "$*"; INSTALLED_COUNT=$((INSTALLED_COUNT + 1)); }  
info() { echo -e "${BLUE}[i]${NC} $*"; _log_write "SKIP" "$*"; SKIPPED_COUNT=$((SKIPPED_COUNT + 1)); }  
warn() { echo -e "${YELLOW}[!]${NC} $*" >&2; _log_write "WARN" "$*"; WARN_COUNT=$((WARN_COUNT + 1)); }  
err()  { echo -e "${RED}[✗]${NC} $*" >&2; _log_write "ERROR" "$*"; ERROR_COUNT=$((ERROR_COUNT + 1)); exit 1; }  
step() { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; _log_write "STEP" "$*"; }  
  
# ---------- non-interactive mode ----------  
export DIZERCORE_NON_INTERACTIVE="${DIZERCORE_NON_INTERACTIVE:-0}"  
  
# Read one line from the controlling terminal (falls back to stdin).  
# Returns non-zero if no input channel is available — callers must handle it,  
# never silently treat "no input" as "user pressed Enter".  
_tty_read() {  
  local __var="$1" __line  
  if read -r __line < /dev/tty 2>/dev/null; then  
    printf -v "$__var" '%s' "$__line"; return 0  
  elif [[ ! -t 0 ]] && read -r __line 2>/dev/null; then  
    printf -v "$__var" '%s' "$__line"; return 0  
  elif read -r __line; then  
    printf -v "$__var" '%s' "$__line"; return 0  
  fi  
  return 1  
}  
  
_tty_print() { printf "%b" "$*" > /dev/tty 2>/dev/null || printf "%b" "$*"; }  
  
ask() {  
  local prompt="$1" default="${2:-}"  
  if [[ "$DIZERCORE_NON_INTERACTIVE" == "1" ]]; then  
    echo "$default"; return 0  
  fi  
  local response  
  if [[ -n "$default" ]]; then  
    _tty_print "${YELLOW}${prompt}${NC} [${default}]: "  
  else  
    _tty_print "${YELLOW}${prompt}${NC}: "  
  fi  
  if ! _tty_read response; then  
    warn "No input available for '${prompt}' — using default '${default}'"  
    echo "$default"; return 0  
  fi  
  response="${response#"${response%%[![:space:]]*}"}"   # trim leading whitespace  
  response="${response%"${response##*[![:space:]]}"}"   # trim trailing whitespace  
  echo "${response:-$default}"  
}  
  
confirm() {  
  local prompt="$1" default="${2:-n}" response  
  if [[ "$DIZERCORE_NON_INTERACTIVE" == "1" ]]; then  
    [[ "$default" == "y" ]] && return 0 || return 1  
  fi  
  local hint="[y/N]"  
  [[ "$default" == "y" ]] && hint="[Y/n]"  
  _tty_print "${YELLOW}${prompt}${NC} ${hint}: "  
  if ! _tty_read response; then  
    warn "Could not read confirmation for '${prompt}' — defaulting to '${default}'"  
    _log_write "PROMPT" "confirm() read failed; default '${default}' used"  
    [[ "$default" == "y" ]] && return 0 || return 1  
  fi  
  response="${response#"${response%%[![:space:]]*}"}"  
  response="${response%"${response##*[![:space:]]}"}"  
  _log_write "PROMPT" "confirm('${prompt}') raw='${response}'"  
  response="${response:-$default}"  
  response="${response,,}"  
  [[ "$response" == "y" || "$response" == "yes" ]]  
}  
  
# ---------- core paths ----------  
export NVME_DEV="/dev/nvme0n1"  
export DATA_MOUNT="/data"  
export GITEA_DIR="${DATA_MOUNT}/gitea"  
export GITEA_DB_DIR="${DATA_MOUNT}/postgres"  
export DOCKER_DATA="${DATA_MOUNT}/docker"  
export CONTAINERD_DATA="${DATA_MOUNT}/containerd"  
export MODELS_DIR="${DATA_MOUNT}/models"  
export PROMPT_HISTORY="${DATA_MOUNT}/prompt-history"  
export WEB_UI_DIR="${DATA_MOUNT}/web-ui"  
export VENV_DIR="${DATA_MOUNT}/venvs/webui"  
export LLAMA_DIR="${DATA_MOUNT}/llama.cpp"  
export REFERENCE_DIR="${DATA_MOUNT}/reference"  
export LOGO_DIR="${DATA_MOUNT}/branding"  
export SRC_DIR="${DATA_MOUNT}/dizercore-src"  
  
# ---------- components ----------  
export GITEA_VERSION="1.25.5"  
export POSTGRES_VERSION="16-alpine"  
  
# ---------- model registry ----------  
# Pipe-delimited entries. Fields (in order):  
#   key | display name | size | speed | GGUF filename | download URL  
# The `key` is what's saved to MODEL_CHOICE_FILE and used by the update path.  
export MODEL_REGISTRY=(  
  "0.5b|Qwen2.5-Coder-0.5B|380 MB|~25 tok/s|qwen2.5-coder-0.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"  
  "0.5b-base|Qwen2.5-0.5B-Instruct|0.4 GB|~45 tok/s|qwen2.5-0.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"  
  "1.5b|Qwen2.5-Coder-1.5B|1.0 GB|~8 tok/s|qwen2.5-coder-1.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"  
  "1.5b-base|Qwen2.5-1.5B-Instruct|1.1 GB|~25 tok/s|qwen2.5-1.5b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"  
  "3b|Qwen2.5-Coder-3B|1.9 GB|~3 tok/s|qwen2.5-coder-3b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF/resolve/main/qwen2.5-coder-3b-instruct-q4_k_m.gguf"  
  "3b-base|Qwen2.5-3B-Instruct|1.9 GB|~18 tok/s|qwen2.5-3b-instruct-q4_k_m.gguf|https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf"  
)  
  
# Where the user's choice is persisted. Read by non-interactive (update) runs.  
export MODEL_CHOICE_FILE="${DATA_MOUNT}/.dizercore-model"  
export MODEL_DEFAULT_KEY="1.5b-base"  
  
# Defaults (overridden by select_model() in lib/04-llama.sh)  
export MODEL_FILE="${MODELS_DIR}/qwen2.5-1.5b-instruct-q4_k_m.gguf"  
export MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"  
  
# ---------- llama-server ----------  
export LLAMA_SERVER_HOST="127.0.0.1"  
export LLAMA_SERVER_PORT="8080"  
export LLAMA_SERVER_URL="http://${LLAMA_SERVER_HOST}:${LLAMA_SERVER_PORT}"  
  
# ---------- repo root ----------  
export REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"  
  
# ---------- derived user + network ----------  
export REAL_USER="${SUDO_USER:-pi}"  
export REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)  
export PI_HOST_IP=$(hostname -I | awk '{print $1}')  
export PI_HOST="$PI_HOST_IP"  
  
# ---------- final report ----------  
print_report() {  
  local exit_code=$?  
  echo ""  
  echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"  
  echo -e "${CYAN}  Install Report${NC}"  
  echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"  
  printf "  ${GREEN}%-12s${NC} %d\n" "Installed:" "$INSTALLED_COUNT"  
  printf "  ${BLUE}%-12s${NC} %d\n" "Skipped:"   "$SKIPPED_COUNT"  
  printf "  ${YELLOW}%-12s${NC} %d\n" "Warnings:"  "$WARN_COUNT"  
  if [[ $ERROR_COUNT -gt 0 ]]; then  
    printf "  ${RED}%-12s${NC} %d\n" "Errors:" "$ERROR_COUNT"  
    echo ""  
    echo -e "  ${RED}Last error:${NC}"  
    grep '\[ERROR' "$LOG_FILE" 2>/dev/null | tail -1 | sed 's/^/    /'  
  else  
    printf "  ${GREEN}%-12s${NC} %d\n" "Errors:" "$ERROR_COUNT"  
  fi  
  echo ""  
  echo -e "  ${DIM}Full log: ${LOG_FILE}${NC}"  
  echo -e "  ${DIM}Exit code: ${exit_code}${NC}"  
  echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"  
  echo ""  
  {  
    echo "==============================================================="  
    echo "Install Report"  
    echo "  Installed: $INSTALLED_COUNT"  
    echo "  Skipped:   $SKIPPED_COUNT"  
    echo "  Warnings:  $WARN_COUNT"  
    echo "  Errors:    $ERROR_COUNT"  
    echo "  Exit code: $exit_code"  
    echo "  Log:       $LOG_FILE"  
    echo "==============================================================="  
  } >> "$LOG_FILE" 2>/dev/null || true  
}  
  
print_summary() {  
  local line1="╔═══════════════════════════════════════════════════════════════════╗"  
  local line2="╚═══════════════════════════════════════════════════════════════════╝"  
  echo ""  
  echo -e "${CYAN}${line1}${NC}"  
  echo -e "${CYAN}║${NC}                                                                   ${CYAN}║${NC}"  
  echo -e "${CYAN}║${NC}                    ${BOLD}${MAGENTA}DizerCore AI Assistant${NC}                         ${CYAN}║${NC}"  
  echo -e "${CYAN}║${NC}                      ${BOLD}· Setup Complete ·${NC}                           ${CYAN}║${NC}"  
  echo -e "${CYAN}║${NC}                                                                   ${CYAN}║${NC}"  
  echo -e "${CYAN}${line2}${NC}"  
  echo ""  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}Web UI${NC}       ${BLUE}http://%s:5000${NC}\n" "$PI_HOST_IP"  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}Gitea${NC}        ${BLUE}http://%s:3000${NC}  ${DIM}(SSH: 2222)${NC}\n" "$PI_HOST_IP"  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}llama-server${NC} %s ${DIM}(model resident in RAM)${NC}\n" "$LLAMA_SERVER_URL"  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}Model${NC}        %s\n" "$MODEL_FILE"  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}History${NC}      %s\n" "$PROMPT_HISTORY/history.json"  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}Reference${NC}    %s\n" "$REFERENCE_DIR"  
  printf "  ${BOLD}${GREEN}●${NC} ${BOLD}Log${NC}          %s\n" "$LOG_FILE"  
  echo ""  
  echo -e "  ${BOLD}${YELLOW}Next Steps${NC}"  
  echo -e "  ${DIM}─────────────────────────────────────────────────${NC}"  
  echo -e "  ${GREEN}1.${NC} Log out and back in  ${DIM}(activates docker group)${NC}"  
  echo -e "  ${GREEN}2.${NC} Open the Web UI and start refining prompts"  
  echo -e "  ${GREEN}3.${NC} Create a repo in Gitea and push your code"  
  echo -e "  ${GREEN}4.${NC} Copy refined prompts into your AI coding agent"  
  echo ""  
  echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"  
  echo -e "  ${DIM}DizerCore AI Assistant · Powered by llama.cpp · Qwen · Gitea · Flask${NC}"  
  echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"  
  echo ""  
}
