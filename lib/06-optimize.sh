#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    lib/06-optimize.sh
# Purpose: Configure ZRAM swap, sysctl tuning, and offer interactive Pi 5
#          overclocking. Overclock is skipped in non-interactive (update) mode.
# =============================================================================

install_optimize() {
  # ---------- ZRAM ----------
  log "Configuring ZRAM ..."
  cat > /etc/default/zramswap <<EOF
ALGO=zstd
PERCENT=50
PRIORITY=100
EOF
  systemctl restart zramswap 2>/dev/null || true

  if ! grep -q "vm.swappiness" /etc/sysctl.conf; then
    echo "vm.swappiness=80" >> /etc/sysctl.conf
  fi
  sysctl -p 2>/dev/null || true
  log "ZRAM configured (50% of RAM, zstd compression)"

  if ! grep -q "${DATA_MOUNT}.*noatime" /etc/fstab; then
    warn "${DATA_MOUNT} mount may not have noatime — check /etc/fstab"
  else
    log "NVMe mounted with noatime"
  fi

  # ---------- overclock (interactive only) ----------
  if [[ "$DIZERCORE_NON_INTERACTIVE" == "1" ]]; then
    info "Non-interactive mode — skipping overclock prompt"
  else
    configure_overclock
  fi
}

# Offer to overclock the Pi 5. Writes to /boot/firmware/config.txt.
# Requires reboot to take effect, so the current session runs at stock speed.
configure_overclock() {
  local config="/boot/firmware/config.txt"
  [[ -f "$config" ]] || config="/boot/config.txt"
  [[ -f "$config" ]] || { warn "config.txt not found — skipping overclock"; return 0; }

  # Already overclocked? Skip.
  if grep -qE '^arm_freq=' "$config" 2>/dev/null; then
    info "Pi already has an arm_freq set in ${config} — skipping overclock prompt"
    return 0
  fi

  echo ""
  echo -e "  ${BOLD}Optional:${NC} Overclock the Pi 5 CPU for faster inference."
  echo -e "  ${DIM}A reboot is required for changes to take effect.${NC}"
  echo -e "  ${DIM}Active cooling (fan) is strongly recommended above 2.6 GHz.${NC}"
  echo ""

  if ! confirm "Overclock the Pi 5?" "n"; then
    info "Overclock skipped — Pi runs at stock 2.4 GHz"
    return 0
  fi

  echo ""
  echo -e "  ${BOLD}Select a profile:${NC}"
  echo -e "    ${GREEN}1)${NC} Conservative  ${DIM}— 2600 MHz CPU, mild voltage bump. Safe with any decent heatsink.${NC}"
  echo -e "    ${GREEN}2)${NC} Aggressive    ${DIM}— 2800 MHz CPU, higher voltage. Needs active cooling.${NC}"
  echo ""

  local profile
  profile=$(ask "Profile [1]" "1")

  # Back up config.txt before editing.
  cp "$config" "${config}.dizercore-backup.$(date +%s)"

  case "$profile" in
    2)
      cat >> "$config" <<EOF

# DizerCore overclock (aggressive)
# Reboot required. Monitor temp in the Web UI. Throttle at 80°C.
# Aggressive becomes:  
arm_boost=1  
arm_freq=2800  
gpu_freq=900  
over_voltage=2  
EOF
      log "Applied aggressive overclock — 2.8 GHz CPU, 900 MHz GPU"
      warn "Reboot required. Watch CPU temp — above 80°C means throttling (add better cooling)"
      ;;
    *)
      cat >> "$config" <<EOF

# DizerCore overclock (conservative)
# Reboot required. Monitor temp in the Web UI. Throttle at 80°C.
# Conservative becomes:  
arm_boost=1  
arm_freq=2600  
gpu_freq=850  
over_voltage=1
EOF
      log "Applied conservative overclock — 2.6 GHz CPU, 850 MHz GPU"
      warn "Reboot required for overclock to take effect"
      ;;
  esac

  # Optional: enable the fan curve (Pi 5 active cooler)
  if ! grep -q "dtparam=fan_temp" "$config" 2>/dev/null; then
    cat >> "$config" <<EOF

# DizerCore fan curve (aggressive cooling)
dtparam=fan_temp0=55000
dtparam=fan_temp0_hyst=5000
dtparam=fan_temp0_speed=128
dtparam=fan_temp1=65000
dtparam=fan_temp1_hyst=5000
dtparam=fan_temp1_speed=200
dtparam=fan_temp2=75000
dtparam=fan_temp2_hyst=5000
dtparam=fan_temp2_speed=255
EOF
    log "Fan curve configured (kicks in at 55°C, full speed at 75°C)"
  fi

  info "Reboot with: sudo reboot"
}
