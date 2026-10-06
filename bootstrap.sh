#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    bootstrap.sh
# Purpose: Entry point that ensures install.sh runs inside tmux so it
#          survives SSH disconnects. Re-execs with ALL THREE file
#          descriptors pointing to /dev/tty, which tmux requires.
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/vekzla/DizerCore-AI.Assistant.git"
BRANCH="main"
SESSION_NAME="dizercore-install"
SELF_URL="https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/${BRANCH}/bootstrap.sh"

# ---------- re-exec with a FULL TTY if stdin is a pipe ----------
# tmux needs stdin, stdout, AND stderr all pointing to the same TTY.
# `curl | bash` breaks this because stdin becomes a pipe.
if [[ ! -t 0 ]] || [[ ! -t 1 ]] || [[ ! -t 2 ]]; then
  if [[ ! -e /dev/tty ]]; then
    echo "ERROR: No controlling terminal. SSH in interactively and try again."
    exit 1
  fi
  TMP_SCRIPT=$(mktemp /tmp/dizercore-bootstrap.XXXXXX.sh)
  echo "[i] Re-executing with full TTY ..."
  curl -fsSL "$SELF_URL" -o "$TMP_SCRIPT"
  chmod +x "$TMP_SCRIPT"
  # Redirect all three fds to /dev/tty before exec.
  exec sudo bash "$TMP_SCRIPT" < /dev/tty > /dev/tty 2>&1
fi

# ---------- root check ----------
if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash bootstrap.sh"
  exit 1
fi

# ---------- already inside tmux? ----------
if [[ -n "${TMUX:-}" ]]; then
  echo "[i] Already inside tmux — running installer directly"
  exec bash <(curl -fsSL "https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/${BRANCH}/install.sh")
fi

# ---------- install tmux if missing ----------
if ! command -v tmux &>/dev/null; then
  echo "[+] Installing tmux ..."
  apt-get update -qq
  apt-get install -y -qq tmux
fi

# ---------- existing session check ----------
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo ""
  echo "[!] A DizerCore install session already exists."
  echo ""
  echo "    Attach:  tmux attach -t $SESSION_NAME"
  echo "    Kill:    tmux kill-session -t $SESSION_NAME"
  echo ""
  exit 0
fi

# ---------- launch install in tmux ----------
echo ""
echo "================================================================"
echo "  DizerCore install is starting in a tmux session"
echo "================================================================"
echo ""
echo "  Session:  ${SESSION_NAME}"
echo ""
echo "  Detach (leave it running):  Ctrl+B, then D"
echo "  Reattach:                   tmux attach -t ${SESSION_NAME}"
echo "  Kill session:               tmux kill-session -t ${SESSION_NAME}"
echo ""
echo "  The install takes 10-25 minutes (mostly compiling llama.cpp)."
echo "  You can safely close this terminal."
echo ""
echo "  Attaching now ..."
echo ""
sleep 3

tmux new-session -s "$SESSION_NAME" \
  "curl -fsSL 'https://raw.githubusercontent.com/vekzla/DizerCore-AI.Assistant/${BRANCH}/install.sh' | bash; echo; echo '=== Installer exited. Ctrl+B, D to leave. ==='; exec bash"
