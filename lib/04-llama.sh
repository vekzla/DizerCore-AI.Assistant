#!/usr/bin/env bash  
# =============================================================================  
# DizerCore AI Assistant  
# -----------------------------------------------------------------------------  
# File:    lib/04-llama.sh  
# Purpose: Clone and build llama.cpp for ARM64, then select and download  
#          a GGUF model. Model choice is persisted to /data/.dizercore-model  
#          so non-interactive (update) runs use the same model.  
# =============================================================================  
  
install_llama() {  
  # ---------- model selection (interactive or persisted) ----------  
  select_model  
  
  # ---------- clone llama.cpp ----------  
  log "Building llama.cpp for ARM64 ..."  
  if [[ ! -d "$LLAMA_DIR/.git" ]]; then  
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"  
  fi  
  
  pushd "$LLAMA_DIR" > /dev/null || err "Cannot cd into ${LLAMA_DIR}"  
  
  # ---------- build ----------  
  if [[ ! -f "build/bin/llama-cli" ]]; then  
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16 -DLLAMA_CURL=ON  
    cmake --build build --config Release -j4  
    log "llama.cpp built successfully"  
  else  
    info "llama.cpp already built — skipping."  
  fi  
  
  popd > /dev/null  
  
  # ---------- model download ----------  
  log "Checking model ..."  
  if [[ -f "$MODEL_FILE" && -s "$MODEL_FILE" ]]; then  
    local size  
    size=$(du -h "$MODEL_FILE" | cut -f1)  
    info "Model already present at ${MODEL_FILE} (${size})"  
  else  
    if [[ -f "$MODEL_FILE" ]]; then  
      warn "Removing partial model file at ${MODEL_FILE}"  
      rm -f "$MODEL_FILE"  
    fi  
  
    download_model "$MODEL_URL" "$MODEL_FILE"  
  fi  
  
  chown -R "${REAL_USER}:${REAL_USER}" "$LLAMA_DIR" "$MODELS_DIR"  
}  
  
# =============================================================================  
# download_model — fetch a GGUF via a .part file so interrupted runs can  
# resume instead of restarting a 1 GB download from zero. Retries 3 times.  
# =============================================================================  
  
download_model() {  
  local url="$1"  
  local dest="$2"  
  local tmp="${dest}.part"  
  local attempt  
  
  log "Downloading model from ${url}"  
  
  for attempt in 1 2 3; do  
    # -c resumes $tmp if it exists; if the server ignores the range  
    # request wget fails, so fall back to a fresh download attempt.  
    if wget --show-progress -c -O "$tmp" "$url"; then  
      : # ok  
    elif wget --show-progress -O "$tmp" "$url"; then  
      : # server refused resume — fresh fetch succeeded  
    else  
      warn "Download attempt ${attempt}/3 failed"  
      sleep 3  
      continue  
    fi  
  
    local size_bytes  
    size_bytes=$(stat -c%s "$tmp" 2>/dev/null || echo 0)  
    if [[ "$size_bytes" -lt 10000000 ]]; then  
      warn "Attempt ${attempt}/3: file too small (${size_bytes} bytes)"  
      sleep 3  
      continue  
    fi  
  
    # Atomic promote — MODEL_FILE only ever holds a complete download  
    mv -f "$tmp" "$dest"  
    local size  
    size=$(du -h "$dest" | cut -f1)  
    log "Model downloaded to ${dest} (${size})"  
    return 0  
  done  
  
  rm -f "$tmp"  
  err "Model download failed after 3 attempts. URL: ${url}  
Check your network, then re-run:  
  cd ${SRC_DIR:-/data/dizercore-src} && sudo bash install.sh"  
}  
  
# =============================================================================  
# select_model — pick a model interactively, or read the saved choice.  
# Sets MODEL_FILE and MODEL_URL in the current shell.  
# =============================================================================  
  
select_model() {  
  local saved_key=""  
  if [[ -f "$MODEL_CHOICE_FILE" ]]; then  
    saved_key=$(tr -d '[:space:]' < "$MODEL_CHOICE_FILE")  
  fi  
  
  # ---------- non-interactive: use saved or default ----------  
  if [[ "$DIZERCORE_NON_INTERACTIVE" == "1" ]]; then  
    local use_key="${saved_key:-$MODEL_DEFAULT_KEY}"  
    info "Non-interactive mode — using saved model: ${use_key}"  
    apply_model_choice "$use_key"  
    return 0  
  fi  
  
  # ---------- interactive: show menu ----------  
  echo ""  
  echo -e "  ${BOLD}Choose the model to install${NC}"  
  echo -e "  ${DIM}Larger models give better output but are slower on the Pi.${NC}"  
  echo ""  
  
  local keys=()  
  local i=1  
  for entry in "${MODEL_REGISTRY[@]}"; do  
    IFS='|' read -r key name size speed file url <<< "$entry"  
    keys+=("$key")  
  
    local tags=""  
    [[ "$key" == "$MODEL_DEFAULT_KEY" ]] && tags="${tags}  ${GREEN}★ recommended${NC}"  
    [[ "$key" == "$saved_key" ]] && tags="${tags}  ${DIM}(currently installed)${NC}"  
  
    printf "    ${GREEN}%d)${NC}  %-26s  %-8s  %-11s%s\n" "$i" "$name" "$size" "$speed" "$tags"  
    i=$((i + 1))  
  done  
  
  echo ""  
  
  # Default index: saved choice if valid, else MODEL_DEFAULT_KEY  
  local default_idx=1  
  if [[ -n "$saved_key" ]]; then  
    for idx in "${!keys[@]}"; do  
      if [[ "${keys[$idx]}" == "$saved_key" ]]; then  
        default_idx=$((idx + 1))  
        break  
      fi  
    done  
  else  
    for idx in "${!keys[@]}"; do  
      if [[ "${keys[$idx]}" == "$MODEL_DEFAULT_KEY" ]]; then  
        default_idx=$((idx + 1))  
        break  
      fi  
    done  
  fi  
  
  local choice  
  choice=$(ask "Model [$default_idx]" "$default_idx")  
  
  # ---------- validate ----------  
  local chosen_key=""  
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#keys[@]} )); then  
    chosen_key="${keys[$((choice - 1))]}"  
  elif [[ " ${keys[*]} " == *" $choice "* ]]; then  
    chosen_key="$choice"  
  else  
    warn "Invalid choice '${choice}' — using default (${MODEL_DEFAULT_KEY})"  
    chosen_key="$MODEL_DEFAULT_KEY"  
  fi  
  
  # ---------- persist ----------  
  mkdir -p "$(dirname "$MODEL_CHOICE_FILE")"  
  echo "$chosen_key" > "$MODEL_CHOICE_FILE"  
  chown "${REAL_USER}:${REAL_USER}" "$MODEL_CHOICE_FILE" 2>/dev/null || true  
  
  apply_model_choice "$chosen_key"  
  
  # ---------- optional cleanup of other models ----------  
  cleanup_other_models "$chosen_key"  
}  
  
# =============================================================================  
# apply_model_choice — set MODEL_FILE and MODEL_URL based on the key.  
# =============================================================================  
  
apply_model_choice() {  
  local key="$1"  
  for entry in "${MODEL_REGISTRY[@]}"; do  
    IFS='|' read -r k name size speed file url <<< "$entry"  
    if [[ "$k" == "$key" ]]; then  
      export MODEL_FILE="${MODELS_DIR}/${file}"  
      export MODEL_URL="$url"  
      log "Selected model: ${name} (${size}, ${speed})"  
      return 0  
    fi  
  done  
  err "Unknown model key: ${key}"  
}  
  
# =============================================================================  
# cleanup_other_models — offer to remove any previously downloaded GGUF files  
# that don't match the current choice. Only runs interactively.  
# =============================================================================  
  
cleanup_other_models() {  
  local keep_key="$1"  
  local keep_file=""  
  for entry in "${MODEL_REGISTRY[@]}"; do  
    IFS='|' read -r k name size speed file url <<< "$entry"  
    if [[ "$k" == "$keep_key" ]]; then  
      keep_file="$file"  
      break  
    fi  
  done  
  [[ -z "$keep_file" ]] && return 0  
  
  local stale=()  
  while IFS= read -r -d '' f; do  
    local base  
    base=$(basename "$f")  
    [[ "$base" == "$keep_file" ]] && continue  
    stale+=("$f")  
  done < <(find "$MODELS_DIR" -maxdepth 1 -type f -name "*.gguf" -print0 2>/dev/null)  
  
  if [[ ${#stale[@]} -eq 0 ]]; then  
    return 0  
  fi  
  
  local total_size  
  total_size=$(du -ch "${stale[@]}" 2>/dev/null | tail -1 | cut -f1)  
  
  echo ""  
  echo -e "  ${BOLD}Old model files found:${NC}"  
  for f in "${stale[@]}"; do  
    printf "    ${DIM}•${NC} %s (%s)\n" "$(basename "$f")" "$(du -h "$f" | cut -f1)"  
  done  
  echo ""  
  if confirm "Delete ${#stale[@]} old model file(s) (~${total_size})?" "n"; then  
    for f in "${stale[@]}"; do  
      rm -f "$f"  
      info "Deleted $(basename "$f")"  
    done  
  else  
    info "Keeping old model files"  
  fi  
}
