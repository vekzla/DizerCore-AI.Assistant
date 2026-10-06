#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    lib/03-gitea.sh
# Purpose: Deploy Gitea + PostgreSQL. Installs the DizerCore logo as both
#          the Gitea header logo and the favicon.
# =============================================================================

install_gitea() {
  log "Setting up Gitea ${GITEA_VERSION} ..."
  cd "$GITEA_DIR"

  PW_FILE="${DATA_MOUNT}/gitea-db-password.txt"
  if [[ -f "$PW_FILE" ]]; then
    DB_PASS=$(cat "$PW_FILE")
    info "Reusing existing Gitea DB password"
  else
    DB_PASS=$(openssl rand -hex 16)
    echo "$DB_PASS" > "$PW_FILE"
    chmod 600 "$PW_FILE"
    chown "${REAL_USER}:${REAL_USER}" "$PW_FILE"
    log "Generated Gitea DB password → ${PW_FILE}"
  fi

  install_branding

  cat > docker-compose.yml <<EOF
services:
  gitea:
    image: gitea/gitea:${GITEA_VERSION}
    container_name: gitea
    environment:
      - USER_UID=1000
      - USER_GID=1000
      - GITEA__database__DB_TYPE=postgres
      - GITEA__database__HOST=db:5432
      - GITEA__database__NAME=gitea
      - GITEA__database__USER=gitea
      - GITEA__database__PASSWD=${DB_PASS}
      - GITEA__server__ROOT_URL=http://\${PI_HOST}:3000/
      - GITEA__server__SSH_PORT=2222
      - GITEA__server__SSH_DOMAIN=\${PI_HOST}
    restart: unless-stopped
    volumes:
      - ${GITEA_DIR}:/data
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
    ports:
      - "3000:3000"
      - "2222:22"
    depends_on:
      db:
        condition: service_healthy
    networks: [gitea-net]

  db:
    image: postgres:${POSTGRES_VERSION}
    container_name: gitea-db
    restart: unless-stopped
    environment:
      - POSTGRES_USER=gitea
      - POSTGRES_PASSWORD=${DB_PASS}
      - POSTGRES_DB=gitea
    volumes:
      - ${GITEA_DB_DIR}:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "gitea"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks: [gitea-net]
    command: >
      postgres -c shared_buffers=128MB -c effective_cache_size=256MB
      -c work_mem=4MB -c maintenance_work_mem=64MB -c max_connections=20

networks:
  gitea-net:
EOF

  export PI_HOST="$PI_HOST_IP"
  docker compose pull -q
  docker compose up -d
  log "Gitea starting at http://${PI_HOST_IP}:3000"
}

# Copy the DizerCore logo into Gitea's custom public assets.
install_branding() {
  local src_logo="${REPO_ROOT}/web-ui/static/DizerCoreYNoBGAI.png"

  if [[ ! -f "$src_logo" ]]; then
    warn "No logo found at ${src_logo} — skipping Gitea branding"
    return 0
  fi

  log "Installing DizerCore logo into Gitea ..."
  mkdir -p "${GITEA_DIR}/custom/public/img"

  cp "$src_logo" "${GITEA_DIR}/custom/public/img/logo.png"
  cp "$src_logo" "${GITEA_DIR}/custom/public/img/favicon.png"

  mkdir -p "${GITEA_DIR}/custom/conf"
  if [[ ! -f "${GITEA_DIR}/custom/conf/app.ini" ]]; then
    cat > "${GITEA_DIR}/custom/conf/app.ini" <<EOF
[ui]
CUSTOM_LOGO = /assets/img/logo.png
EOF
  fi

  chown -R "${REAL_USER}:${REAL_USER}" "${GITEA_DIR}/custom"
  log "Gitea logo installed"
}
