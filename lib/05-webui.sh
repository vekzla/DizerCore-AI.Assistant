#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    lib/05-webui.sh  
# Purpose: Copy the Flask app to NVMe, install the DizerCore logo, create  
#          a virtualenv, register the systemd services (Web UI + index  
#          watcher), allow passwordless updates, and start both services.  
# =============================================================================  
  
install_webui() {  
  log "Setting up Flask Web UI ..."  
  mkdir -p "$WEB_UI_DIR"  
  
  cp -r "${REPO_ROOT}/web-ui/." "$WEB_UI_DIR/"  
  mkdir -p "${WEB_UI_DIR}/static"  
  
  install_webui_logo  
  
  # ---------- API token ----------  
  # Shared secret the Web UI requires on every /api/* request. Written once,  
  # reused on re-installs so existing browsers keep working.  
  if [[ ! -f "${WEB_UI_DIR}/.api-token" ]]; then  
    cat /proc/sys/kernel/random/uuid | tr -d '-' > "${WEB_UI_DIR}/.api-token"  
    chmod 600 "${WEB_UI_DIR}/.api-token"  
  fi  
  
  if [[ ! -d "$VENV_DIR" ]]; then  
    python3 -m venv "$VENV_DIR"  
  fi  
  "${VENV_DIR}/bin/pip" install --upgrade pip -q  
  "${VENV_DIR}/bin/pip" install -r "${WEB_UI_DIR}/requirements.txt" -q  
  
  # ---------- Web UI systemd unit ----------  
  cat > /etc/systemd/system/prompt-gateway.service <<EOF  
[Unit]  
Description=DizerCore AI Assistant Web UI  
After=network-online.target llama-server.service  
Wants=network-online.target llama-server.service  
  
[Service]  
Type=simple  
User=${REAL_USER}  
Group=${REAL_USER}  
WorkingDirectory=${WEB_UI_DIR}  
Environment="MODEL_PATH=${MODEL_FILE}"  
Environment="LLAMA_BIN=${LLAMA_DIR}/build/bin/llama-cli"  
Environment="LLAMA_SERVER_URL=${LLAMA_SERVER_URL}"  
Environment="HISTORY_DIR=${PROMPT_HISTORY}"  
Environment="SRC_DIR=${SRC_DIR}"  
Environment="LOG_FILE=${LOG_FILE}"  
Environment="REFERENCE_DIR=${REFERENCE_DIR}"  
Environment="INDEX_DB=${WEB_UI_DIR}/dizercore-index.db"  
Environment="ACTIVITY_FILE=${WEB_UI_DIR}/.ai-activity"  
Environment="WATCHER_URL=http://127.0.0.1:8091"  
Environment="TRAINING_DIR=/data/training"  
Environment="MODELS_DIR=${MODELS_DIR}"  
Environment="WEB_UI_DIR=${WEB_UI_DIR}"  
Environment="API_TOKEN_FILE=${WEB_UI_DIR}/.api-token"  
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"  
ExecStart=${VENV_DIR}/bin/python ${WEB_UI_DIR}/app.py  
Restart=on-failure  
RestartSec=5  
  
[Install]  
WantedBy=multi-user.target  
EOF  
  
  # ---------- index watcher systemd unit ----------  
  cat > /etc/systemd/system/index-watcher.service <<EOF  
[Unit]  
Description=DizerCore Reference Index Watcher  
After=network-online.target prompt-gateway.service  
Wants=network-online.target prompt-gateway.service  
  
[Service]  
Type=simple  
User=${REAL_USER}  
Group=${REAL_USER}  
WorkingDirectory=${WEB_UI_DIR}  
Environment="REFERENCE_DIR=${REFERENCE_DIR}"  
Environment="INDEX_DB=${WEB_UI_DIR}/dizercore-index.db"  
Environment="ACTIVITY_FILE=${WEB_UI_DIR}/.ai-activity"  
Environment="WATCH_INTERVAL=300"  
Environment="WATCH_PORT=8091"  
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"  
ExecStart=${VENV_DIR}/bin/python ${WEB_UI_DIR}/index-watcher.py  
Restart=on-failure  
RestartSec=10  
  
[Install]  
WantedBy=multi-user.target  
EOF  
  
 # ---------- sudoers rules ----------  
  cat > /etc/sudoers.d/dizercore-update <<EOF  
${REAL_USER} ALL=(ALL) NOPASSWD: /bin/bash ${SRC_DIR}/install.sh  
${REAL_USER} ALL=(ALL) NOPASSWD: ${SRC_DIR}/install.sh  
${REAL_USER} ALL=(ALL) NOPASSWD: /usr/local/sbin/dizercore-training-deploy  
EOF 
  chmod 440 /etc/sudoers.d/dizercore-update  
  
  chown -R "${REAL_USER}:${REAL_USER}" "$WEB_UI_DIR" "$PROMPT_HISTORY"  
  
  systemctl daemon-reload  
  systemctl enable prompt-gateway.service index-watcher.service  
  systemctl restart prompt-gateway.service  
  systemctl restart index-watcher.service  
  log "Web UI running at http://${PI_HOST_IP}:5000"  
  log "Index watcher running (checks every 5 min)"  
}  
  
# Copy the DizerCore logo into the Flask static directory.  
install_webui_logo() {  
  local src_logo="${REPO_ROOT}/web-ui/static/DizerCoreYNoBGAI.png"  
  
  if [[ ! -f "$src_logo" ]]; then  
    warn "No logo found at ${src_logo} — Web UI will show no logo"  
    return 0  
  fi  
  
  mkdir -p "${WEB_UI_DIR}/static"  
  cp "$src_logo" "${WEB_UI_DIR}/static/logo.png"  
  chown "${REAL_USER}:${REAL_USER}" "${WEB_UI_DIR}/static/logo.png"  
  log "Web UI logo installed"  
}
