#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    lib/09-llama-server.sh
# Purpose: Register llama-server systemd unit and wait for the model to load.
#          Context window is 8192 so the model has room for retrieved file
#          windows (4 files × ~3000 chars).
#
#          --cache-reuse 256: reuse the KV cache across requests when the
#          prompt prefix (system prompt + game-data.txt) matches the previous
#          request. Cuts prefill from ~115s to ~15s on every request after
#          the first.
# =============================================================================

install_llama_server() {
  log "Setting up llama-server ..."
  [[ -x "${LLAMA_DIR}/build/bin/llama-server" ]] || err "llama-server binary not found"
  [[ -f "$MODEL_FILE" ]] || err "Model file not found at ${MODEL_FILE}"

  cat > /etc/systemd/system/llama-server.service <<EOF
[Unit]
Description=llama.cpp Server (DizerCore)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REAL_USER}
Group=${REAL_USER}
Environment="HOME=${REAL_HOME}"
WorkingDirectory=${LLAMA_DIR}
ExecStart=${LLAMA_DIR}/build/bin/llama-server \\
  -m ${MODEL_FILE} \\
  --host ${LLAMA_SERVER_HOST} \\
  --port ${LLAMA_SERVER_PORT} \\
  --threads 4 \\
  --ctx-size 8192 \\
  --n-predict 400 \\
  --temp 0.3 \\
  --cache-reuse 256 \\
  --no-webui
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable llama-server.service
  systemctl restart llama-server.service

  log "Waiting for model to load ..."
  local ready=0
  for i in $(seq 1 90); do
    curl -sf "http://${LLAMA_SERVER_HOST}:${LLAMA_SERVER_PORT}/health" >/dev/null 2>&1 && { ready=1; break; }
    sleep 1
  done

  if [[ $ready -eq 1 ]]; then
    local rss=$(systemctl show llama-server.service -p MainPID --value | xargs -I{} ps -o rss= -p {} 2>/dev/null | awk '{printf "%.0f", $1/1024}')
    log "llama-server ready — model in RAM (~${rss:-?} MB, ctx 8192)"
  else
    warn "llama-server did not respond within 90s"
  fi
}
