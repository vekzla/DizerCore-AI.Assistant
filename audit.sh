#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant — System Audit
# Read-only. Paste the output back for diagnostics.
# =============================================================================
set -uo pipefail

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'
B='\033[0;34m'; C='\033[0;36m'; D='\033[2m'; N='\033[0m'

OK()   { printf "${G}  ✓${N} %s\n" "$*"; }
FAIL() { printf "${R}  ✗${N} %s\n" "$*"; }
WARN() { printf "${Y}  !${N} %s\n" "$*"; }
HDR()  { printf "\n${C}━━━ %s ━━━${N}\n" "$*"; }
NOTE() { printf "${D}    %s${N}\n" "$*"; }

REPORT="/tmp/dizercore-audit-$(date +%Y%m%d-%H%M%S).txt"
exec > >(tee "$REPORT") 2>&1

echo "═══════════════════════════════════════════════════════════════════"
echo "  DizerCore AI Assistant — System Audit"
echo "  Generated: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Hostname:  $(hostname)"
echo "═══════════════════════════════════════════════════════════════════"

# 1. OS
HDR "1/14: Operating System"
[[ -f /etc/os-release ]] && { . /etc/os-release; OK "OS: ${PRETTY_NAME:-unknown}"; } || WARN "Cannot read /etc/os-release"
OK "Kernel: $(uname -r)"
OK "Architecture: $(uname -m)"
OK "Uptime: $(uptime -p 2>/dev/null || uptime)"

# 2. Hardware
HDR "2/14: Hardware"
[[ -f /proc/device-tree/model ]] && OK "Model: $(tr -d '\0' < /proc/device-tree/model)"
OK "RAM: $(free -h | awk '/^Mem:/{print $3}') / $(free -h | awk '/^Mem:/{print $2}')"
CPU_FREQ=$(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2 || echo "0")
[[ "$CPU_FREQ" -gt 0 ]] && OK "CPU clock: $((CPU_FREQ / 1000000)) MHz"
TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -oP '\d+\.\d+' || echo "0")
OK "CPU temp: ${TEMP}°C"
THROTTLE=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo "n/a")
case "$THROTTLE" in
  0x0)   OK "Throttle: clean ($THROTTLE)" ;;
  n/a)   WARN "Throttle: vcgencmd unavailable" ;;
  *)     WARN "Throttle: $THROTTLE (check bits)" ;;
esac

# 3. Storage
HDR "3/14: Storage"
if mountpoint -q /data 2>/dev/null; then
  DF=$(df -h /data | tail -1)
  OK "/data mounted: $(echo "$DF" | awk '{print $3}') / $(echo "$DF" | awk '{print $2}') ($(echo "$DF" | awk '{print $5}'))"
else
  FAIL "/data NOT mounted"
fi
[[ -b /dev/nvme0n1 ]] && OK "NVMe device present: /dev/nvme0n1" || FAIL "NVMe device /dev/nvme0n1 missing"
grep -q "/data" /etc/fstab 2>/dev/null && OK "/data in /etc/fstab" || FAIL "/data not in /etc/fstab"

# 4. Base packages
HDR "4/14: Base packages (apt)"
for pkg in curl git python3 python3-venv python3-pip jq parted ripgrep cmake \
           ca-certificates gnupg zram-tools e2fsprogs sqlite3; do
  dpkg -l "$pkg" 2>/dev/null | grep -q "^ii" && OK "$pkg" || WARN "$pkg (missing)"
done

# 5. Docker
HDR "5/14: Docker"
for pkg in docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; do
  dpkg -l "$pkg" 2>/dev/null | grep -q "^ii" && OK "$pkg" || FAIL "$pkg (missing)"
done
if command -v docker &>/dev/null; then
  OK "docker: $(docker --version)"
  docker compose version &>/dev/null && OK "compose: $(docker compose version --short 2>/dev/null)" || FAIL "compose plugin broken"
  DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "")
  [[ "$DOCKER_ROOT" == "/data/docker" ]] && OK "Docker data-root → /data/docker" || WARN "Docker data-root: $DOCKER_ROOT (expected /data/docker)"
fi
[[ -f /etc/apt/sources.list.d/docker.list ]] && OK "Docker apt repo configured" || FAIL "Docker apt repo missing"

if [[ -f /etc/containerd/config.toml ]]; then
  CT_ROOT=$(grep -E '^\s*root\s*=' /etc/containerd/config.toml 2>/dev/null | head -1 | sed 's/.*= *//; s/"//g')
  if [[ "$CT_ROOT" == "/data/containerd" ]]; then
    OK "containerd root → /data/containerd"
  elif [[ -z "$CT_ROOT" ]]; then
    WARN "containerd root: not set (uses default /var/lib/containerd on SD card)"
  else
    WARN "containerd root: $CT_ROOT (expected /data/containerd)"
  fi
fi

# 6. Systemd services
HDR "6/14: Systemd services"
for svc in llama-server prompt-gateway index-watcher; do
  unit_file="/etc/systemd/system/${svc}.service"
  if [[ -f "$unit_file" ]]; then
    STATE=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    ENABLED=$(systemctl is-enabled "$svc" 2>/dev/null || echo "disabled")
    if [[ "$STATE" == "active" ]]; then
      OK "$svc: $STATE ($ENABLED)"
    else
      FAIL "$svc: $STATE ($ENABLED)"
      journalctl -u "$svc" -n 5 --no-pager 2>/dev/null | sed 's/^/      /' || true
    fi
  else
    FAIL "$svc.service unit file missing at $unit_file"
  fi
done

# 7. llama-server
HDR "7/14: llama-server"
if [[ -f /etc/systemd/system/llama-server.service ]]; then
  EXEC=$(grep -A1 "^ExecStart=" /etc/systemd/system/llama-server.service | head -3 | tr '\n' ' ' | sed 's/\\//g' | tr -s ' ')
  MODEL_PATH=$(echo "$EXEC" | grep -oP '(?<= -m )\S+' | head -1)
  OK "Active model: ${MODEL_PATH:-unknown}"
  [[ -n "$MODEL_PATH" && -f "$MODEL_PATH" ]] && OK "Model file exists: $(du -h "$MODEL_PATH" | cut -f1)" || FAIL "Model file missing"
fi
curl -sf http://127.0.0.1:8080/health >/dev/null 2>&1 && OK "llama-server /health OK" || FAIL "llama-server /health down"

# 8. llama.cpp build
HDR "8/14: llama.cpp build"
if [[ -x /data/llama.cpp/build/bin/llama-cli ]]; then
  OK "llama-cli: $(/data/llama.cpp/build/bin/llama-cli --version 2>&1 | head -1)"
else
  FAIL "llama-cli binary missing"
fi
for bin in llama-server llama-quantize; do
  [[ -x "/data/llama.cpp/build/bin/$bin" ]] && OK "$bin present" || WARN "$bin missing"
done

# 9. Web UI files
HDR "9/14: Web UI"
if [[ -d /data/web-ui ]]; then
  OK "/data/web-ui exists"
  for f in app.py training.py indexer.py index-watcher.py training-deploy.sh game-data.txt requirements.txt; do
    [[ -f "/data/web-ui/$f" ]] && OK "  $f" || FAIL "  $f missing"
  done
  [[ -f /data/web-ui/templates/index.html ]] && OK "  templates/index.html" || FAIL "  templates/index.html missing"
  [[ -f /data/web-ui/static/logo.png ]] && OK "  static/logo.png" || WARN "  static/logo.png missing"
else
  FAIL "/data/web-ui missing"
fi
curl -sf http://127.0.0.1:5000/ >/dev/null 2>&1 && OK "Web UI on :5000" || FAIL "Web UI not responding"

# 10. Python venv
HDR "10/14: Python venv + packages"
if [[ -x /data/venvs/webui/bin/python ]]; then
  OK "venv: $(/data/venvs/webui/bin/python --version)"
  for pkg in flask flask-cors psutil; do
    if /data/venvs/webui/bin/pip show "$pkg" >/dev/null 2>&1; then
      V=$(/data/venvs/webui/bin/pip show "$pkg" 2>/dev/null | grep "^Version:" | awk '{print $2}')
      OK "  $pkg ($V)"
    else
      FAIL "  $pkg missing"
    fi
  done
else
  FAIL "venv missing at /data/venvs/webui"
fi

# 11. Gitea + PostgreSQL
HDR "11/14: Gitea + PostgreSQL"
if command -v docker &>/dev/null; then
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^gitea$" && OK "Gitea running ($(docker inspect gitea --format '{{.Config.Image}}' 2>/dev/null))" || FAIL "Gitea not running"
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^gitea-db$" && OK "PostgreSQL running ($(docker inspect gitea-db --format '{{.Config.Image}}' 2>/dev/null))" || FAIL "PostgreSQL not running"
fi
[[ -f /data/gitea-db-password.txt ]] && OK "Gitea DB password file" || WARN "Gitea DB password file missing"
[[ -f /data/gitea/docker-compose.yml ]] && OK "Gitea docker-compose.yml" || FAIL "Gitea docker-compose.yml missing"

# 12. Reference / index / training
HDR "12/14: Reference / index / training"
if [[ -d /data/reference ]] && [[ -n "$(ls -A /data/reference 2>/dev/null)" ]]; then
  for d in /data/reference/*/; do
    [[ -d "$d/.git" ]] || continue
    NAME=$(basename "$d")
    REPO_OWNER=$(stat -c %U "$d" 2>/dev/null || echo "pi")  
    BRANCH=$(sudo -u "$REPO_OWNER" git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
    SIZE=$(du -sh "$d" 2>/dev/null | cut -f1)
    OK "$NAME (branch: $BRANCH, $SIZE)"
  done
else
  WARN "No reference repo"
fi

INDEX_DB="/data/web-ui/dizercore-index.db"
if [[ -f "$INDEX_DB" ]]; then
  SIZE=$(du -h "$INDEX_DB" | cut -f1)
  MTIME=$(stat -c %y "$INDEX_DB" | cut -d. -f1)
  COUNT=$(/data/venvs/webui/bin/python -c "import sqlite3; print(sqlite3.connect('$INDEX_DB').execute('SELECT count(*) FROM files').fetchone()[0])" 2>/dev/null || echo "?")
  OK "FTS5 index: $SIZE (${COUNT} files, built $MTIME)"
else
  WARN "FTS5 index not built"
fi

[[ -f /data/training/dizercore-dataset.jsonl ]] && OK "Dataset: $(du -h /data/training/dizercore-dataset.jsonl | cut -f1) ($(wc -l < /data/training/dizercore-dataset.jsonl) examples)" || WARN "Dataset not built"
[[ -f /data/models/dizercore-q4_k_m.gguf ]] && OK "Trained model: $(du -h /data/models/dizercore-q4_k_m.gguf | cut -f1)" || WARN "Trained model not present"

# 13. Config files
HDR "13/14: Config files"
for f in /etc/sudoers.d/dizercore-update /etc/docker/daemon.json /etc/default/zramswap; do
  [[ -f "$f" ]] && OK "$f" || WARN "$f missing"
done
grep -q "vm.swappiness" /etc/sysctl.conf 2>/dev/null && OK "sysctl: $(grep vm.swappiness /etc/sysctl.conf)" || WARN "vm.swappiness not set"
[[ -f /boot/firmware/config.txt ]] && grep -qE "^arm_freq=" /boot/firmware/config.txt && OK "Overclock: $(grep ^arm_freq /boot/firmware/config.txt)" || WARN "No overclock"

# 14. Logs
HDR "14/14: Logs"
for log in /var/log/dizercore-install.log /var/log/dizercore-uninstall.log; do
  if [[ -f "$log" ]]; then
    OK "$log ($(du -h "$log" | cut -f1), $(wc -l < "$log") lines)"
    NOTE "  Errors: $(grep -c '\[ERROR' "$log" 2>/dev/null || echo 0)  Warnings: $(grep -c '\[WARN' "$log" 2>/dev/null || echo 0)"
  else
    WARN "$log (missing)"
  fi
done

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  Report saved: $REPORT"
echo "═══════════════════════════════════════════════════════════════════"
