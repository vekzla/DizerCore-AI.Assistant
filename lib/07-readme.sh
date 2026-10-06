#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    lib/07-readme.sh
# Purpose: Write a runtime README to /data/README.md containing paths, URLs,
#          usage instructions, and system info gathered at install time.
# =============================================================================

install_readme() {
  # The heredoc interpolates all the exported vars from common.sh.
  # Command substitutions ($(hostname), $(uname -r), etc.) run at write time.
  cat > "${DATA_MOUNT}/README.md" <<EOF
# Pi Prompt Gateway

A self-contained prompt-engineering workstation running on a Raspberry Pi 5 (8GB) with NVMe storage.

## What's Installed

| Component | Path | Purpose |
|---|---|---|
| **Gitea** | \`http://${PI_HOST_IP}:3000\` | Local Git server for your repos |
| **Web UI** | \`http://${PI_HOST_IP}:5000\` | Prompt refinement interface |
| **llama.cpp** | \`${LLAMA_DIR}\` | ARM64-optimized LLM inference |
| **Model** | \`${MODEL_FILE}\` | Qwen2.5-Coder-0.5B-Instruct |
| **Prompt History** | \`${PROMPT_HISTORY}\` | Every refined prompt |
| **NVMe Data** | \`${DATA_MOUNT}\` | All non-OS storage |

## How to Use

1. Open \`http://${PI_HOST_IP}:5000\` in your browser.
2. Type your rough prompt (e.g., "fix spell target positions in SpellMgr.cpp").
3. Click **Refine Prompt**.
4. Wait ~10 seconds for the model to process.
5. Click **Copy** to copy the refined prompt.
6. Paste into Devin Cloud or Devin Desktop.
7. When Devin is done, push changes to Gitea.

## Web UI Features

- **Refine Prompt**: Send your rough prompt to the local LLM.
- **Copy**: Copy the refined prompt to your clipboard.
- **History**: Every prompt is saved to the NVMe. Click any entry to view it.
- **Clear All**: Wipe the history.
- **Set Logo**: Upload a custom logo for the header.

## Management

\`\`\`bash
# Web UI
sudo systemctl status prompt-gateway
sudo systemctl restart prompt-gateway
sudo journalctl -u prompt-gateway -f

# Gitea
cd ${GITEA_DIR}
docker compose ps
docker compose logs -f gitea

# Model
ls -lh ${MODEL_FILE}

# History
cat ${PROMPT_HISTORY}/history.json | jq .
\`\`\`

## Re-running the Installer

The installer is idempotent. Running it again will:
- Skip NVMe formatting if already mounted.
- Skip Docker install if already present.
- Skip model download if already present.
- Preserve existing prompt history and Gitea data.

To re-run individual steps, source the lib file and call the function:
\`\`\`bash
cd /tmp/pi-prompt-gateway  # or wherever you cloned
source lib/common.sh
source lib/05-webui.sh
install_webui
\`\`\`

## System Info

- **Hostname**: $(hostname)
- **IP**: ${PI_HOST_IP}
- **OS**: $(lsb_release -ds 2>/dev/null || grep PRETTY_NAME /etc/os-release | cut -d= -f2)
- **Kernel**: $(uname -r)
- **RAM**: $(free -h | awk '/^Mem:/{print $2}')
- **NVMe**: $(df -h ${DATA_MOUNT} | tail -1 | awk '{print $2}')
EOF

  chown "${REAL_USER}:${REAL_USER}" "${DATA_MOUNT}/README.md"
  log "Runtime README written to ${DATA_MOUNT}/README.md"
}
