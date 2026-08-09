#!/bin/bash
# deploy.sh - Application deployment for McApp bootstrap
# Handles: slot-based deployment, release tarball, webapp, version management, systemd

#──────────────────────────────────────────────────────────────────
# SLOT MANAGEMENT (3 slots: slot-0, slot-1, slot-2 + current symlink)
#──────────────────────────────────────────────────────────────────

# Initialize slot layout — creates directories, handles migration from legacy ~/mcapp
init_slot_layout() {
  local run_user="${SUDO_USER:-$(whoami)}"
  local run_home
  run_home=$(get_real_home)

  # Create slot directories
  mkdir -p "${SLOTS_DIR}/slot-0" "${SLOTS_DIR}/slot-1" "${SLOTS_DIR}/slot-2" "${META_DIR}"
  chown -R "$run_user:$run_user" "$SLOTS_DIR"

  # Migrate legacy ~/mcapp into slot-0 if it exists and slots aren't set up yet
  local legacy_dir="${run_home}/mcapp"
  if [[ -d "$legacy_dir" ]] && [[ -f "${legacy_dir}/pyproject.toml" ]] && \
     [[ ! -L "${SLOTS_DIR}/current" ]]; then
    log_info "  Migrating legacy ~/mcapp into slot-0..."

    # Move contents into slot-0
    if [[ -d "${SLOTS_DIR}/slot-0" ]]; then
      rm -rf "${SLOTS_DIR}/slot-0"
    fi
    mv "$legacy_dir" "${SLOTS_DIR}/slot-0"

    # Read version from migrated slot
    local migrated_version="unknown"
    if [[ -f "${SLOTS_DIR}/slot-0/webapp/version.html" ]]; then
      migrated_version=$(cat "${SLOTS_DIR}/slot-0/webapp/version.html" 2>/dev/null)
    fi

    # Write slot metadata
    write_slot_meta 0 "$migrated_version" "active"

    # Create current symlink
    ln -sfn "slot-0" "${SLOTS_DIR}/current"

    # Snapshot /etc files for rollback
    snapshot_etc_files 0

    chown -R "$run_user:$run_user" "$SLOTS_DIR"

    # Rebuild venv (shebangs reference old ~/mcapp path)
    local uv_bin="${run_home}/.local/bin/uv"
    if [[ -x "$uv_bin" ]]; then
      sudo -u "$run_user" bash -c "cd '${SLOTS_DIR}/slot-0' && '${uv_bin}' sync --all-packages" || true
    fi

    log_ok "  Legacy installation migrated to slot-0 (${migrated_version})"
  fi

  # Ensure 'current' symlink exists (first install: point to slot-0)
  if [[ ! -L "${SLOTS_DIR}/current" ]]; then
    ln -sfn "slot-0" "${SLOTS_DIR}/current"
    log_info "  Created initial 'current' symlink → slot-0"
  fi
}

# Read slot metadata JSON
read_slot_meta() {
  local slot_id="$1"
  local meta_file="${META_DIR}/slot-${slot_id}.json"
  if [[ -f "$meta_file" ]]; then
    cat "$meta_file"
  else
    echo '{"slot":'"$slot_id"',"version":null,"status":"empty","deployed_at":null}'
  fi
}

# Write slot metadata JSON
write_slot_meta() {
  local slot_id="$1"
  local version="$2"
  local status="${3:-active}"
  local deployed_at
  deployed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  cat > "${META_DIR}/slot-${slot_id}.json" << SLOTEOF
{
  "slot": ${slot_id},
  "version": "${version}",
  "status": "${status}",
  "deployed_at": "${deployed_at}"
}
SLOTEOF
}

# Get the slot ID that 'current' symlink points to
get_active_slot() {
  local current="${SLOTS_DIR}/current"
  if [[ -L "$current" ]]; then
    local target
    target=$(readlink "$current")
    echo "${target#slot-}"
  else
    echo ""
  fi
}

# Find the oldest (or empty) slot for new deployment
get_target_slot() {
  local active
  active=$(get_active_slot)

  # Prefer empty slots
  local i
  for i in 0 1 2; do
    local meta_file="${META_DIR}/slot-${i}.json"
    if [[ ! -f "$meta_file" ]] || ! jq -e '.version' "$meta_file" &>/dev/null; then
      echo "$i"
      return
    fi
    local version
    version=$(jq -r '.version // ""' "$meta_file" 2>/dev/null)
    if [[ -z "$version" || "$version" == "null" ]]; then
      echo "$i"
      return
    fi
  done

  # All slots used — pick oldest non-active
  local oldest_slot=""
  local oldest_date="9999"
  for i in 0 1 2; do
    [[ "$i" == "$active" ]] && continue
    local date
    date=$(jq -r '.deployed_at // "0"' "${META_DIR}/slot-${i}.json" 2>/dev/null)
    # String comparison is INTENDED here and `-lt` would be wrong: these are
    # ISO-8601 timestamps ("2026-07-30T21:15:04Z") plus the "9999"/"0"
    # sentinels, none of which are integers. ISO-8601 sorts correctly
    # lexicographically, which is the whole point of that format.
    # shellcheck disable=SC2071
    if [[ "$date" < "$oldest_date" ]]; then
      oldest_date="$date"
      oldest_slot="$i"
    fi
  done

  echo "${oldest_slot:-0}"
}

# Resolve the release tag we intend to run for the given mode.
resolve_target_version() {
  local dev_mode="${1:-false}"
  local pin_tag="${2:-}"
  if [[ -n "$pin_tag" ]]; then
    echo "$pin_tag"
  elif [[ "$dev_mode" == "true" ]]; then
    get_latest_prerelease_version
  else
    get_latest_release_version
  fi
}

# Version recorded in a slot's metadata (empty when unknown/absent).
slot_recorded_version() {
  local slot_id="${1:-}"
  [[ -z "$slot_id" ]] && return
  jq -r '.version // ""' "${META_DIR}/slot-${slot_id}.json" 2>/dev/null || true
}

# Snapshot /etc config files for rollback
snapshot_etc_files() {
  local slot_id="$1"
  local archive="${META_DIR}/slot-${slot_id}.etc.tar.gz"
  local -a files_to_backup=()

  # Same leak class as migrate_config()'s `key`: these libs are sourced into
  # mcapp.sh's shell, so an undeclared loop variable clobbers the caller's.
  local path
  for path in \
    /etc/mcapp/config.json \
    /etc/systemd/system/mcapp.service \
    /etc/systemd/system/mcapp-ble.service \
    /etc/lighttpd/conf-available/99-mcapp.conf \
    /etc/lighttpd/lighttpd.conf
  do
    [[ -f "$path" ]] && files_to_backup+=("$path")
  done

  if (( ${#files_to_backup[@]} > 0 )); then
    tar czf "$archive" "${files_to_backup[@]}" 2>/dev/null || true
    log_info "  Snapshotted /etc config files for slot-${slot_id}"
  fi
}

# Atomically swap the 'current' symlink to a new slot
swap_current_symlink() {
  local slot_id="$1"
  local tmp_link="${SLOTS_DIR}/.current.tmp"
  rm -f "$tmp_link"
  ln -s "slot-${slot_id}" "$tmp_link"
  mv -Tf "$tmp_link" "${SLOTS_DIR}/current"
  log_info "  Activated slot-${slot_id}"
}

#──────────────────────────────────────────────────────────────────
# MAIN DEPLOYMENT FUNCTION
#──────────────────────────────────────────────────────────────────

deploy_app() {
  local force="${1:-false}"
  local dev_mode="${2:-false}"
  local pin_tag="${3:-}"
  local remote_version="${4:-}"

  # Initialize slot layout (creates dirs, migrates legacy if needed)
  init_slot_layout

  # Capture old version before deployment
  local old_version
  old_version=$(get_installed_mcapp_version)

  # remote_version is normally resolved once by the caller (mcapp.sh's
  # resolve_install_ref(), via MCAPP_INSTALL_APP_VERSION) and passed in, so
  # this does not make a second, independently-resolved API call — a release
  # cut between the two calls would otherwise give libs from tag A and app
  # from tag B (§3.1). Resolve here too so deploy_app stays usable standalone.
  local active_slot active_version
  if [[ -z "$remote_version" ]]; then
    remote_version=$(resolve_target_version "$dev_mode" "$pin_tag")
  fi
  active_slot=$(get_active_slot)
  active_version=$(slot_recorded_version "$active_slot")

  # Decide whether to ROTATE to a fresh slot or deploy IN PLACE on the active
  # one. Rotate only when genuinely rolling a different version forward (or
  # forcing, or the active slot is missing/broken). A plain idempotent re-run —
  # active slot already at the target version — must NOT rotate: get_target_slot
  # returns the oldest NON-active slot, whose stale contents would then be
  # activated and silently downgrade the box (exactly how a re-run rolled the
  # backend from dev.5 back to a stale dev.3 slot).
  local rotate=false
  if [[ "$force" == "true" ]]; then
    rotate=true
  elif [[ "$remote_version" == "unknown" ]]; then
    rotate=false
  elif [[ -z "$active_slot" ]]; then
    rotate=true
  elif [[ "${active_version#v}" != "${remote_version#v}" ]]; then
    rotate=true
  elif [[ ! -f "${SLOTS_DIR}/slot-${active_slot}/pyproject.toml" ]]; then
    rotate=true
  fi

  if [[ "$rotate" == "true" ]]; then
    DEPLOY_SLOT=$(get_target_slot)
  else
    DEPLOY_SLOT="${active_slot:-$(get_target_slot)}"
    [[ -n "$active_slot" ]] && \
      log_info "  Active slot-${active_slot} already at ${remote_version} — deploying in place (no rotation)"
  fi
  local deploy_target="${SLOTS_DIR}/slot-${DEPLOY_SLOT}"
  log_info "  Deploy target: slot-${DEPLOY_SLOT} (${deploy_target})"

  # Snapshot current /etc files before making changes
  if [[ -n "$active_slot" ]]; then
    snapshot_etc_files "$active_slot"
  fi

  # Deploy into target slot (pass the already-resolved remote version)
  deploy_release "$force" "$dev_mode" "$deploy_target" "$pin_tag" "$remote_version"
  deploy_webapp "$force" "$deploy_target" "$pin_tag"

  # Guard: if target slot is still empty after deploy steps (e.g. version
  # unchanged and slot was never populated), stay on the current slot.
  # Without this, setup_python_env would fail on the empty directory.
  if [[ ! -f "${deploy_target}/pyproject.toml" ]]; then
    log_info "  Nothing to deploy — staying on current slot"
    export MCAPP_OLD_VERSION="$old_version"
    export MCAPP_NEW_VERSION="$old_version"
    return 0
  fi

  setup_python_env "$deploy_target"
  migrate_config
  deploy_shell_aliases

  # Read new version from deployed slot
  local new_version
  if [[ -f "${deploy_target}/webapp/version.html" ]]; then
    new_version=$(cat "${deploy_target}/webapp/version.html" 2>/dev/null)
  else
    new_version=$(get_installed_mcapp_version)
  fi

  # Write slot metadata
  write_slot_meta "$DEPLOY_SLOT" "$new_version" "active"

  # Swap current symlink to new slot
  swap_current_symlink "$DEPLOY_SLOT"

  # Store versions for service restart logging
  export MCAPP_OLD_VERSION="$old_version"
  export MCAPP_NEW_VERSION="$new_version"
}

#──────────────────────────────────────────────────────────────────
# RELEASE TARBALL DEPLOYMENT
#──────────────────────────────────────────────────────────────────

# Query GitHub Releases API for the latest stable release tag
get_latest_release_version() {
  local tag
  tag=$(curl -fsSL --connect-timeout 5 \
    "${GITHUB_API_BASE}/releases/latest" 2>/dev/null \
    | jq -r '.tag_name // empty' 2>/dev/null)

  if [[ -n "$tag" ]]; then
    echo "$tag"
  else
    echo "unknown"
  fi
}

# Query GitHub Releases API for the latest pre-release tag
# Sorts by version number (sort -V) instead of relying on API ordering
get_latest_prerelease_version() {
  local tag
  tag=$(curl -fsSL --connect-timeout 5 \
    "${GITHUB_API_BASE}/releases?per_page=100" 2>/dev/null \
    | jq -r '.[] | select(.prerelease) | .tag_name' 2>/dev/null \
    | sort -V | tail -1)

  if [[ -n "$tag" ]]; then
    echo "$tag"
  else
    echo "unknown"
  fi
}

# Read installed version from version.html (contains full tag like v1.01.1-dev.14)
get_installed_mcapp_version() {
  # Primary: version.html from deployed webapp
  if [[ -f "${WEBAPP_DIR}/version.html" ]]; then
    cat "${WEBAPP_DIR}/version.html" 2>/dev/null || echo "not_installed"
    return
  fi

  # Fallback: version.html bundled in active slot
  if [[ -f "${INSTALL_DIR}/webapp/version.html" ]]; then
    cat "${INSTALL_DIR}/webapp/version.html" 2>/dev/null || echo "not_installed"
    return
  fi

  echo "not_installed"
}

deploy_release() {
  local force="${1:-false}"
  local dev_mode="${2:-false}"
  local deploy_target="${3:-$INSTALL_DIR}"
  local pin_tag="${4:-}"
  local remote_version="${5:-}"

  log_info "Checking McApp release deployment..."

  local installed_version
  installed_version=$(get_installed_mcapp_version)

  # remote_version is normally resolved once by the caller (deploy_app) and
  # passed in; resolve here too so deploy_release stays usable standalone.
  if [[ -z "$remote_version" ]]; then
    remote_version=$(resolve_target_version "$dev_mode" "$pin_tag")
  fi
  if [[ -n "$pin_tag" ]]; then
    log_info "  Mode: pinned tag"
  elif [[ "$dev_mode" == "true" ]]; then
    log_info "  Mode: development (pre-release)"
  fi

  # What the TARGET slot currently holds. Activation points mcapp.service at
  # this slot, so whether to (re)download is decided from the SLOT's own
  # version vs the release we want — NOT the served webapp (WEBAPP_DIR), which
  # can legitimately differ and previously masked a stale slot as "up to date"
  # (that let a re-run activate an older slot and downgrade the backend).
  local target_version=""
  [[ -f "${deploy_target}/webapp/version.html" ]] && \
    target_version=$(cat "${deploy_target}/webapp/version.html" 2>/dev/null)
  local target_has_code=false
  [[ -f "${deploy_target}/pyproject.toml" ]] && target_has_code=true

  log_info "  Installed: ${installed_version}"
  log_info "  Remote:    ${remote_version}"

  if [[ "$force" == "true" ]]; then
    log_info "  Force mode: reinstalling ${remote_version}"
  elif [[ "$remote_version" == "unknown" ]]; then
    if [[ "$target_has_code" == "true" ]]; then
      log_warn "  Cannot determine remote version — leaving target slot as-is"
      return 0
    fi
    log_error "  Cannot determine remote version and target slot is empty"
    return 1
  elif [[ "$target_has_code" == "true" ]] && [[ "${target_version#v}" == "${remote_version#v}" ]]; then
    log_info "  McApp is up to date (slot already ${remote_version})"
    return 0
  else
    log_info "  Deploying ${remote_version} (slot had: ${target_version:-empty})"
  fi

  download_and_install_release "$remote_version" "$deploy_target"
}

download_and_install_release() {
  local version="${1:-}"
  local deploy_target="${2:-$INSTALL_DIR}"

  # If no version given, fetch latest
  if [[ -z "$version" || "$version" == "unknown" ]]; then
    version=$(get_latest_release_version)
    if [[ "$version" == "unknown" ]]; then
      log_error "  Cannot determine latest release version"
      return 1
    fi
  fi

  log_info "  Downloading McApp ${version}..."

  local tarball_name="mcapp-${version}.tar.gz"
  local checksum_name="mcapp-${version}.tar.gz.sha256"
  local release_url="https://github.com/${GITHUB_REPO}/releases/download/${version}"
  local tmp_dir
  tmp_dir=$(mktemp -d)

  # Download release tarball
  if ! curl -fsSL -o "${tmp_dir}/${tarball_name}" "${release_url}/${tarball_name}"; then
    log_error "  Failed to download release tarball"
    rm -rf "$tmp_dir"
    return 1
  fi

  # Download and verify SHA256 checksum
  if curl -fsSL -o "${tmp_dir}/${checksum_name}" "${release_url}/${checksum_name}" 2>/dev/null; then
    log_info "  Verifying SHA256 checksum..."
    if ! (cd "$tmp_dir" && sha256sum -c "$checksum_name" --quiet 2>/dev/null); then
      log_error "  Checksum verification failed!"
      rm -rf "$tmp_dir"
      return 1
    fi
    log_ok "  Checksum verified"
  else
    log_warn "  No checksum file available, skipping verification"
  fi

  # Clean target slot directory (fresh extraction)
  # Note: glob * does not match dotfiles, so remove .venv explicitly
  # to prevent stale shebangs from surviving slot reuse
  rm -rf "${deploy_target:?}"/* "${deploy_target}/.venv"
  mkdir -p "$deploy_target"

  # Extract tarball with --strip-components=1 to remove top-level dir
  tar -xzf "${tmp_dir}/${tarball_name}" -C "$deploy_target" --strip-components=1 --warning=no-unknown-keyword

  # Set ownership to the real user (not root)
  local run_user="${SUDO_USER:-$(whoami)}"
  chown -R "$run_user:$run_user" "$deploy_target"

  # Create runtime directories required by systemd ReadWritePaths
  mkdir -p /var/lib/mcapp
  chown "$run_user:$run_user" /var/lib/mcapp
  mkdir -p /var/log/mcapp
  chown "$run_user:$run_user" /var/log/mcapp

  # Cleanup
  rm -rf "$tmp_dir"

  log_ok "  McApp ${version} deployed to ${deploy_target}"
}

#──────────────────────────────────────────────────────────────────
# WEBAPP DEPLOYMENT
#──────────────────────────────────────────────────────────────────

deploy_webapp() {
  local force="${1:-false}"
  local deploy_target="${2:-$INSTALL_DIR}"
  local pin_tag="${3:-}"

  log_info "Checking webapp deployment..."

  # New flow: if the tarball included webapp/, use it directly
  if [[ -d "${deploy_target}/webapp" ]] && [[ -f "${deploy_target}/webapp/index.html" ]]; then
    deploy_webapp_from_tarball "$force" "$deploy_target" "$pin_tag"
  else
    # Fallback: old tarball without bundled webapp — download separately
    log_info "  No bundled webapp in tarball, falling back to download"
    deploy_webapp_download "$force"
  fi
}

# Deploy webapp from the bundled webapp/ directory in the release tarball
deploy_webapp_from_tarball() {
  local force="${1:-false}"
  local deploy_target="${2:-$INSTALL_DIR}"
  local pin_tag="${3:-}"

  local installed_version
  local tarball_version

  installed_version=$(get_installed_webapp_version)

  # Read version from the bundled webapp
  if [[ -f "${deploy_target}/webapp/version.html" ]]; then
    tarball_version=$(cat "${deploy_target}/webapp/version.html")
  else
    tarball_version="unknown"
  fi

  log_info "  Installed: ${installed_version}"
  log_info "  Bundled:   ${tarball_version}"

  if [[ -n "$pin_tag" ]]; then
    log_info "  Pinned tag: deploying bundled webapp"
  elif [[ "$force" != "true" ]] && [[ "$installed_version" != "not_installed" ]] && \
     [[ "$tarball_version" != "unknown" ]] && \
     [[ "$installed_version" != *-dev* || "$tarball_version" == *-dev* ]] && \
     version_gte "$installed_version" "$tarball_version"; then
    log_info "  Webapp is up to date"
    return 0
  fi

  log_info "  Deploying bundled webapp..."

  # Ensure webapp directory exists
  mkdir -p "$WEBAPP_DIR"

  # Copy from tarball to webapp serve dir
  cp -a "${deploy_target}/webapp/." "$WEBAPP_DIR/"

  # Set permissions
  chown -R www-data:www-data "$WEBAPP_DIR"
  chmod -R 755 "$WEBAPP_DIR"

  log_ok "  Webapp deployed from tarball to ${WEBAPP_DIR}"
}

# Fallback: download webapp separately (backward compat with old releases)
deploy_webapp_download() {
  local force="${1:-false}"

  local installed_version
  local remote_version

  installed_version=$(get_installed_webapp_version)
  remote_version=$(get_remote_webapp_version)

  log_info "  Installed: ${installed_version}"
  log_info "  Remote:    ${remote_version}"

  # Decide if update needed
  if [[ "$force" == "true" ]]; then
    log_info "  Force mode: reinstalling webapp"
  elif [[ "$installed_version" == "not_installed" ]]; then
    log_info "  Webapp not installed, downloading..."
  elif [[ "$remote_version" == "unknown" ]]; then
    log_warn "  Cannot check remote version, skipping update"
    return 0
  elif [[ "$installed_version" == *-dev* ]] && [[ "$remote_version" != *-dev* ]]; then
    log_info "  Switching webapp from dev to production: ${installed_version} → ${remote_version}"
  elif version_gte "$installed_version" "$remote_version"; then
    log_info "  Webapp is up to date"
    return 0
  else
    log_info "  Updating webapp: ${installed_version} → ${remote_version}"
  fi

  download_webapp
}

download_webapp() {
  log_info "  Downloading webapp..."

  local webapp_url="${GITHUB_RAW_BASE}/webapp/webapp.tar.gz"
  local checksum_url="${GITHUB_RAW_BASE}/webapp/webapp.tar.gz.sha256"
  local tmp_dir
  tmp_dir=$(mktemp -d)

  # Download webapp archive
  if ! curl -fsSL -o "${tmp_dir}/webapp.tar.gz" "$webapp_url"; then
    log_error "  Failed to download webapp"
    rm -rf "$tmp_dir"
    return 1
  fi

  # Download and verify checksum (if available)
  if curl -fsSL -o "${tmp_dir}/webapp.tar.gz.sha256" "$checksum_url" 2>/dev/null; then
    log_info "  Verifying checksum..."
    if ! (cd "$tmp_dir" && sha256sum -c webapp.tar.gz.sha256 --quiet 2>/dev/null); then
      log_error "  Checksum verification failed!"
      rm -rf "$tmp_dir"
      return 1
    fi
    log_info "  Checksum verified"
  else
    log_warn "  No checksum available, skipping verification"
  fi

  # Ensure webapp directory exists
  mkdir -p "$WEBAPP_DIR"

  # Extract webapp
  tar -xzf "${tmp_dir}/webapp.tar.gz" -C "$WEBAPP_DIR" --strip-components=1 --warning=no-unknown-keyword

  # Set permissions
  chown -R www-data:www-data "$WEBAPP_DIR"
  chmod -R 755 "$WEBAPP_DIR"

  # Cleanup
  rm -rf "$tmp_dir"

  log_ok "  Webapp deployed to ${WEBAPP_DIR}"
}

#──────────────────────────────────────────────────────────────────
# PYTHON ENVIRONMENT (uv sync --all-packages)
#──────────────────────────────────────────────────────────────────

setup_python_env() {
  local deploy_target="${1:-$INSTALL_DIR}"

  log_info "Setting up Python environment with uv sync --all-packages..."

  if [[ ! -f "${deploy_target}/pyproject.toml" ]]; then
    log_error "  No pyproject.toml found in ${deploy_target}"
    return 1
  fi

  # Resolve uv binary path (may not be in root's PATH)
  local run_user="${SUDO_USER:-$(whoami)}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)
  local uv_bin="${run_home}/.local/bin/uv"

  if [[ ! -x "$uv_bin" ]]; then
    # Fallback: check system PATH
    uv_bin=$(command -v uv 2>/dev/null || true)
    if [[ -z "$uv_bin" ]]; then
      log_error "  uv not found - install it first"
      return 1
    fi
  fi

  # Remove stale venv to avoid shebang mismatch from migrated environments.
  # Scripts like uvicorn have shebangs pointing to the venv's python.
  # If the venv was copied/migrated from another path, shebangs are stale.
  if [[ -d "${deploy_target}/.venv/bin" ]]; then
    # Check ALL executable scripts — any stale shebang triggers full removal.
    # Previously only checked the first script found, which could miss stale
    # third-party scripts (e.g., uvicorn) when project scripts (mcapp) were OK.
    local stale_found=false
    while IFS= read -r script; do
      local shebang
      shebang=$(head -1 "$script" 2>/dev/null || true)
      if [[ "$shebang" == "#!"* && "$shebang" != *"${deploy_target}"* ]]; then
        stale_found=true
        break
      fi
    done < <(find "${deploy_target}/.venv/bin" -maxdepth 1 -type f \( -name 'uvicorn' -o -name 'mcapp' -o -name '*.py' \) 2>/dev/null)
    if [[ "$stale_found" == "true" ]]; then
      log_info "  Removing stale venv (shebangs point elsewhere)"
      rm -rf "${deploy_target}/.venv"
    fi
  fi

  # Run uv sync --all-packages as the real user (not root).
  #
  # The `|| sync_ok=false` is load-bearing twice over. (1) mcapp.sh runs under
  # `set -eo pipefail`, and a bare failing command in an if-BRANCH is fatal
  # (only the if-CONDITION is exempt) — so a failed sync used to abort the
  # whole bootstrap on the spot and the log_error below was unreachable dead
  # code. Putting it in a `||` list makes the failure handled, so the
  # diagnostic actually prints before we return 1. (2) It replaces the old
  # `if [[ $? -eq 0 ]]` (SC2181), which silently breaks the moment anyone
  # inserts so much as a log line above it.
  local sync_ok=true
  if [[ "$run_user" != "root" ]]; then
    sudo -u "$run_user" bash -c "cd '${deploy_target}' && '${uv_bin}' sync --all-packages" \
      || sync_ok=false
  else
    (cd "$deploy_target" && "$uv_bin" sync --all-packages) || sync_ok=false
  fi

  if [[ "$sync_ok" == "true" ]]; then
    log_ok "  Python environment ready (including workspace members)"
  else
    log_error "  uv sync --all-packages failed"
    return 1
  fi
}

#──────────────────────────────────────────────────────────────────
# SERVICE ACTIVATION
#──────────────────────────────────────────────────────────────────

activate_services() {
  log_info "Activating services..."

  disable_conflicting_services
  configure_systemd_service
  enable_and_start_services
}

disable_conflicting_services() {
  # Caddy is the public front door on :80/:443 (see install_caddy()/
  # configure_caddy() in packages.sh, and bootstrap/templates/caddy/
  # Caddyfile.mcapp) — lighttpd runs behind it on 127.0.0.1:8082. This used
  # to stop+disable Caddy here on the theory that it "conflicts with
  # lighttpd on port 80"; that's now inverted: Caddy must stay running, and
  # it's lighttpd that gets moved off :80 (done in configure_lighttpd(),
  # which already ran during install_packages(), earlier than this
  # function in the boot sequence — see mcapp.sh Phase 4 vs Phase 6).
  #
  # Defensive check only: if lighttpd is somehow still bound to :80 (e.g.
  # this run used --skip, which never calls install_packages()/
  # configure_lighttpd() at all), warn loudly instead of silently letting
  # Caddy and lighttpd fight over the port.
  if ss -tlnp 2>/dev/null | grep ':80\b' | grep -q 'lighttpd'; then
    log_warn "  lighttpd is still listening on :80 (expected 127.0.0.1:8082)"
    log_warn "  Re-run mcapp.sh WITHOUT --skip so configure_lighttpd() can migrate the port"
  fi

  # Old mcapp process may still hold port 1799 (e.g. running from old venv/scripts)
  # The systemd-managed mcapp will be restarted by enable_and_start_services,
  # but rogue processes outside systemd need to be killed
  if ss -ulnp 2>/dev/null | grep -q ':1799\b'; then
    local pid
    pid=$(ss -ulnp 2>/dev/null | grep ':1799\b' | grep -oP 'pid=\K\d+' | head -1)
    if [[ -n "$pid" ]]; then
      # Check if this is managed by our systemd service
      local unit
      unit=$(systemctl status "$pid" 2>/dev/null | head -1 || true)
      if [[ "$unit" != *"mcapp.service"* ]]; then
        log_info "  Killing rogue process on UDP port 1799 (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        sleep 1
      fi
    fi
  fi
}

configure_systemd_service() {
  log_info "  Configuring systemd services..."

  local run_user="${SUDO_USER:-root}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)

  # --- mcapp.service ---
  local mcapp_service="/etc/systemd/system/mcapp.service"
  local template_dir

  # Find template directory (prefer newly deployed slot)
  local deploy_target="${SLOTS_DIR}/slot-${DEPLOY_SLOT:-0}"
  if [[ -d "${deploy_target}/bootstrap/templates" ]]; then
    template_dir="${deploy_target}/bootstrap/templates"
  elif [[ -d "${INSTALL_DIR}/bootstrap/templates" ]]; then
    template_dir="${INSTALL_DIR}/bootstrap/templates"
  elif [[ -d "${SCRIPT_DIR}/templates" ]]; then
    template_dir="${SCRIPT_DIR}/templates"
  else
    log_error "  Cannot find service templates"
    return 1
  fi

  # Render mcapp.service from template
  sed -e "s|{{USER}}|${run_user}|g" \
      -e "s|{{HOME}}|${run_home}|g" \
      "${template_dir}/mcapp.service" > "$mcapp_service"

  log_info "  mcapp.service configured"

  # --- mcapp-ble.service (optional) ---
  if [[ -f "${template_dir}/mcapp-ble.service" ]]; then
    local ble_service="/etc/systemd/system/mcapp-ble.service"

    # Read BLE API key from config if available
    local ble_api_key=""
    if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
      ble_api_key=$(jq -r '.BLE_API_KEY // ""' "$CONFIG_FILE" 2>/dev/null)
    fi

    # Escape special chars: & for sed replacement, % for systemd specifiers
    local ble_api_key_escaped="${ble_api_key//&/\\&}"
    ble_api_key_escaped="${ble_api_key_escaped//%/%%}"

    sed -e "s|{{USER}}|${run_user}|g" \
        -e "s|{{HOME}}|${run_home}|g" \
        -e "s|{{BLE_API_KEY}}|${ble_api_key_escaped}|g" \
        "${template_dir}/mcapp-ble.service" > "$ble_service"

    log_info "  mcapp-ble.service configured"
  fi

  # --- mcapp-update.path + mcapp-update.service (file-trigger update) ---
  if [[ -f "${template_dir}/mcapp-update.service" ]]; then
    sed -e "s|{{USER}}|${run_user}|g" \
        -e "s|{{HOME}}|${run_home}|g" \
        "${template_dir}/mcapp-update.service" > /etc/systemd/system/mcapp-update.service
    log_info "  mcapp-update.service configured"
  fi
  if [[ -f "${template_dir}/mcapp-update.path" ]]; then
    cp "${template_dir}/mcapp-update.path" /etc/systemd/system/mcapp-update.path
    log_info "  mcapp-update.path configured"
  fi

  # Clean up legacy sudoers from previous installs
  configure_update_sudoers "$run_user" "$run_home"

  systemctl daemon-reload
}

# Clean up legacy sudoers from previous installs (no longer needed — using .path trigger)
configure_update_sudoers() {
  local run_user="$1"
  local run_home="$2"
  local sudoers_file="/etc/sudoers.d/mcapp-update"

  if [[ -f "$sudoers_file" ]]; then
    rm -f "$sudoers_file"
    log_info "  Removed legacy sudoers file (replaced by mcapp-update.path trigger)"
  fi
}

enable_and_start_services() {
  log_info "  Enabling and starting services..."

  local -a services=("lighttpd" "mcapp-ble" "mcapp" "mcapp-update.path")
  local failed=false
  local old_version="${MCAPP_OLD_VERSION:-unknown}"
  local new_version="${MCAPP_NEW_VERSION:-unknown}"

  local svc
  for svc in "${services[@]}"; do
    # Enable service
    log_info "  Enabling ${svc}..."
    if ! systemctl enable "$svc" 2>/dev/null; then
      log_warn "  Failed to enable ${svc}"
      failed=true
      continue
    fi

    # Restart service (or start if not running)
    if systemctl is-active --quiet "$svc"; then
      # Log maintenance stop for mcapp service
      if [[ "$svc" == "mcapp" ]]; then
        log_deployment_event "MAINTENANCE_START" "$old_version" "$new_version"
      fi

      log_info "  Restarting ${svc}..."
      if ! systemctl restart "$svc" 2>/dev/null; then
        log_warn "  Failed to restart ${svc}"
        failed=true
      else
        log_ok "  ${svc} restarted"
        # Log successful deployment for mcapp service
        if [[ "$svc" == "mcapp" ]]; then
          log_deployment_event "DEPLOYMENT_COMPLETE" "$old_version" "$new_version"
        fi
      fi
    else
      log_info "  Starting ${svc}..."
      if ! systemctl start "$svc" 2>/dev/null; then
        log_warn "  Failed to start ${svc}"
        failed=true
      else
        log_ok "  ${svc} started"
        # Log initial installation for mcapp service
        if [[ "$svc" == "mcapp" ]]; then
          log_deployment_event "INITIAL_INSTALL" "$old_version" "$new_version"
        fi
      fi
    fi
  done

  if [[ "$failed" == "true" ]]; then
    log_warn "  Some services failed to start - check logs"
  else
    log_ok "  All services enabled and started"
  fi
}

# Log deployment events to systemd journal for the mcapp service
# These messages will appear in 'sudo journalctl -u mcapp.service'
log_deployment_event() {
  local event_type="$1"
  local old_version="$2"
  local new_version="$3"

  case "$event_type" in
    MAINTENANCE_START)
      systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Stopping service for maintenance and deployment"
      if [[ "$old_version" != "unknown" && "$old_version" != "not_installed" ]]; then
        systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Current version: ${old_version}"
      fi
      ;;
    DEPLOYMENT_COMPLETE)
      if [[ "$new_version" != "unknown" && "$new_version" != "not_installed" ]]; then
        systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Deployment complete - new version: ${new_version}"
      fi
      if [[ "$old_version" != "$new_version" && "$old_version" != "not_installed" && "$old_version" != "unknown" ]]; then
        systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Upgraded from ${old_version} to ${new_version}"
      fi
      ;;
    INITIAL_INSTALL)
      systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Initial installation complete"
      if [[ "$new_version" != "unknown" && "$new_version" != "not_installed" ]]; then
        systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Installed version: ${new_version}"
      fi
      ;;
  esac
}

#──────────────────────────────────────────────────────────────────
# SHELL ALIASES
#──────────────────────────────────────────────────────────────────

deploy_shell_aliases() {
  local target="/etc/profile.d/mcapp.sh"

  cat > "$target" << 'ALIASES'
# /etc/profile.d/mcapp.sh - McApp convenience aliases
# Deployed by McApp bootstrap — do not edit manually
# Changes will be overwritten on next bootstrap run

alias ll='ls -l'
alias mcapp-sdcard='sudo "$HOME/mcapp-slots/current/bootstrap/sd-card.sh"'
alias mcapp-update='curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash'
alias mcapp-dev-update='curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/development/bootstrap/mcapp.sh | sudo bash -s -- --dev'
ALIASES

  chmod 644 "$target"
  log_info "  Shell aliases deployed to ${target}"
}
