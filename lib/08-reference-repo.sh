#!/usr/bin/env bash
# =============================================================================
# DizerCore AI Assistant
# -----------------------------------------------------------------------------
# File:    lib/08-reference-repo.sh
# Purpose: Prompt the user for a reference Git repository and branch, then
#          clone (or update) it into /data/reference/. After cloning,
#          trigger the index watcher to build the search index.
# =============================================================================

install_reference_repo() {
  # In non-interactive (update) mode, skip the prompt but still trigger a
  # rebuild if the watcher isn't already handling it.
  if [[ "$DIZERCORE_NON_INTERACTIVE" == "1" ]]; then
    if [[ -d "$REFERENCE_DIR" && -n "$(ls -A "$REFERENCE_DIR" 2>/dev/null)" ]]; then
      info "Reference repos already present — skipping clone"
      trigger_index_build
    else
      info "No reference repos — skipping"
    fi
    return 0
  fi

  log "Reference repository setup"

  mkdir -p "$REFERENCE_DIR"
  chown "${REAL_USER}:${REAL_USER}" "$REFERENCE_DIR"

  echo ""
  echo -e "  ${BOLD}Optional:${NC} Clone a reference repository to work against."
  echo ""

  local repo_url
  repo_url=$(ask "Git repository URL (leave blank to skip)")

  if [[ -z "$repo_url" ]]; then
    info "No reference repo specified — skipping"
    return 0
  fi

  local branch
  branch=$(ask "Branch to checkout" "main")

  local repo_name
  repo_name=$(basename "$repo_url" .git)

  local target="${REFERENCE_DIR}/${repo_name}"

  if [[ -d "$target/.git" ]]; then
    info "Reference repo already exists at ${target}"

    log "Fetching latest from origin ..."
    sudo -u "$REAL_USER" git -C "$target" fetch origin --prune 2>/dev/null || \
      warn "Fetch failed — continuing with local state"

    if sudo -u "$REAL_USER" git -C "$target" rev-parse --verify "origin/${branch}" >/dev/null 2>&1; then
      log "Checking out ${branch} ..."
      sudo -u "$REAL_USER" git -C "$target" checkout "$branch" 2>/dev/null || \
        sudo -u "$REAL_USER" git -C "$target" checkout -b "$branch" "origin/${branch}"
      sudo -u "$REAL_USER" git -C "$target" pull --ff-only origin "$branch" 2>/dev/null || \
        warn "Could not fast-forward ${branch} — local changes may exist"
    else
      warn "Branch '${branch}' not found on origin — staying on current branch"
    fi
  else
    log "Cloning ${repo_url} (branch: ${branch}) into ${target} ..."
    if ! sudo -u "$REAL_USER" git clone --branch "$branch" "$repo_url" "$target"; then
      warn "Clone failed — verify the URL and branch name, then try again"
      [[ -d "$target" ]] && rm -rf "$target"
      return 0
    fi
  fi

  local file_count size active_branch commit
  file_count=$(find "$target" -type f -not -path '*/.git/*' 2>/dev/null | wc -l)
  size=$(du -sh "$target" 2>/dev/null | cut -f1)
  active_branch=$(sudo -u "$REAL_USER" git -C "$target" rev-parse --abbrev-ref HEAD 2>/dev/null)
  commit=$(sudo -u "$REAL_USER" git -C "$target" rev-parse --short HEAD 2>/dev/null)

  log "Reference repo ready: ${target}"
  log "  Branch: ${active_branch}"
  log "  Commit: ${commit}"
  log "  ${file_count} files, ${size} on disk"

  trigger_index_build

  echo ""
  if confirm "Clone another reference repository?" "n"; then
    install_reference_repo
  fi
}

# =============================================================================
# trigger_index_build — kick off an index build via the watcher if it's
# running, or spawn the indexer directly as a fallback.
# =============================================================================

trigger_index_build() {
  local indexer="${WEB_UI_DIR}/indexer.py"
  local python="${VENV_DIR}/bin/python"

  if [[ ! -f "$indexer" || ! -x "$python" ]]; then
    info "Indexer or venv not ready — skipping index build"
    return 0
  fi

  # Preferred: ask the watcher to rebuild
  if systemctl is-active --quiet index-watcher.service 2>/dev/null; then
    log "Triggering index rebuild via watcher ..."
    if curl -sf -X POST http://127.0.0.1:8091/trigger >/dev/null 2>&1; then
      info "Watcher accepted trigger — watch: journalctl -u index-watcher -f"
      return 0
    fi
    warn "Watcher didn't respond — falling back to direct build"
  fi

  # Fallback: direct build, only if index doesn't exist yet
  if [[ -f "${WEB_UI_DIR}/dizercore-index.db" ]]; then
    info "Index already exists — watcher will keep it fresh"
    return 0
  fi

  log "Building initial search index in the background ..."
  nohup sudo -u "$REAL_USER" "$python" "$indexer" \
    > /var/log/dizercore-index.log 2>&1 &
  info "Index build started — watch: tail -f /var/log/dizercore-index.log"
}
