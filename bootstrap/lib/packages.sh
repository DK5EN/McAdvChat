#!/bin/bash
# packages.sh - Package management for McApp bootstrap
# Handles: apt packages, uv, lighttpd

#──────────────────────────────────────────────────────────────────
# MAIN PACKAGE INSTALLATION
#──────────────────────────────────────────────────────────────────

install_packages() {
  install_apt_deps
  install_uv
  ensure_web_frontend
}

# Idempotent install + configuration of the web front door.
#
# ORDER MATTERS: install_lighttpd (→ configure_lighttpd) moves lighttpd off
# :80 onto 127.0.0.1:8082 and full-restarts it, freeing :80 BEFORE
# install_caddy (→ configure_caddy) binds :80/:443. Both are idempotent —
# command -v guards + version markers mean steady-state is a no-op with no
# restart — so calling this repeatedly (and from more than one code path) is
# safe.
#
# This exists as its own function because the web-updater path
# (update-runner.py → `mcapp.sh --skip`) never runs install_packages(), so
# without a second call site the :80→8082 move + Caddy install would never
# reach production upgrades. See the --skip call site in mcapp.sh.
ensure_web_frontend() {
  install_lighttpd
  install_caddy
}

#──────────────────────────────────────────────────────────────────
# TEMPORARY SWAP FOR APT OPERATIONS
#──────────────────────────────────────────────────────────────────

readonly APT_SWAP_FILE="/var/tmp/.mcapp-apt-swap"

# Create temporary 256MB swap if none is active.
# Uses /var/tmp (not /tmp which may be tmpfs after system.sh Phase 3).
# Soft failure — logs warning and continues if swap creation fails.
ensure_apt_swap() {
  # Skip if swap is already active (system swap or previous run)
  if [[ $(swapon --show --noheadings 2>/dev/null | wc -l) -gt 0 ]]; then
    log_info "  Swap already active, skipping temporary swap"
    return 0
  fi

  log_info "  Creating temporary 256MB swap for apt operations..."
  if dd if=/dev/zero of="$APT_SWAP_FILE" bs=1M count=256 status=none 2>/dev/null \
     && chmod 600 "$APT_SWAP_FILE" \
     && mkswap "$APT_SWAP_FILE" >/dev/null 2>&1 \
     && swapon "$APT_SWAP_FILE" 2>/dev/null; then
    log_ok "  Temporary swap activated"
  else
    log_warn "  Could not create temporary swap (continuing without it)"
    rm -f "$APT_SWAP_FILE" 2>/dev/null
  fi
}

# Remove temporary swap file (idempotent)
remove_apt_swap() {
  if swapon --show --noheadings 2>/dev/null | grep -q "$APT_SWAP_FILE"; then
    swapoff "$APT_SWAP_FILE" 2>/dev/null
  fi
  rm -f "$APT_SWAP_FILE" 2>/dev/null
}

#──────────────────────────────────────────────────────────────────
# APT LOCK HANDLING
#──────────────────────────────────────────────────────────────────

# Stop unattended-upgrades to prevent apt lock contention during bootstrap.
# Waits up to 60s for any running dpkg process to finish after stopping.
wait_for_apt_lock() {
  if systemctl is-active --quiet unattended-upgrades.service 2>/dev/null; then
    log_info "  Stopping unattended-upgrades to avoid apt lock conflict..."
    systemctl stop unattended-upgrades.service 2>/dev/null
    # Wait for any running dpkg process to finish (max 60s)
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend &>/dev/null && (( waited < 60 )); do
      sleep 2
      waited=$((waited + 2))
    done
    if (( waited > 0 )); then
      log_info "  Waited ${waited}s for dpkg lock to release"
    fi
  fi
}

# Re-enable unattended-upgrades if it was enabled before bootstrap stopped it.
restore_unattended_upgrades() {
  if systemctl is-enabled --quiet unattended-upgrades.service 2>/dev/null; then
    systemctl start unattended-upgrades.service 2>/dev/null
    log_info "  Re-enabled unattended-upgrades"
  fi
}

#──────────────────────────────────────────────────────────────────
# APT DEPENDENCIES
#──────────────────────────────────────────────────────────────────

install_apt_deps() {
  log_info "Installing system dependencies..."

  # Prevent lock contention with unattended-upgrades
  wait_for_apt_lock

  # Update package lists and upgrade installed packages
  apt-get update -qq

  # Temporary swap to prevent OOM during large upgrades (Pi Zero 2W has 512MB)
  ensure_apt_swap

  log_info "Upgrading installed packages..."
  DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
  log_ok "  System packages upgraded"

  # Configure locale AFTER upgrade (glibc/locales upgrade invalidates generated locales)
  configure_locale

  # Core dependencies (no python3-venv or python3-pip — uv handles everything)
  local -a packages=(
    # Essential tools
    "curl"
    "jq"
    "bc"
    "screen"
    "git"

    # Python runtime (uv manages venvs and packages)
    "python3"

    # BlueZ for BLE support (only if running BLE service locally)
    "bluez"
    "bluetooth"

    # For D-Bus (no longer needed - dbus-next removed from main package)
    # "libdbus-1-dev"  # Only needed by standalone BLE service

    # For SSL certificates
    "ca-certificates"
    "gnupg"

    # SD card health diagnostics (mmc extcsd)
    "mmc-utils"
  )

  # Install all packages
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"

  # Clean up temporary swap
  remove_apt_swap

  # Re-enable unattended-upgrades now that apt operations are done
  restore_unattended_upgrades

  log_ok "  System dependencies installed"
}

#──────────────────────────────────────────────────────────────────
# UV PACKAGE MANAGER
#──────────────────────────────────────────────────────────────────

install_uv() {
  log_info "Installing uv package manager..."

  # Check if uv is already available for the real user
  local run_user="${SUDO_USER:-$(whoami)}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)

  if [[ -x "${run_home}/.local/bin/uv" ]]; then
    local uv_version
    uv_version=$("${run_home}/.local/bin/uv" --version 2>/dev/null | head -1)
    log_info "  uv already installed: ${uv_version}"
    return 0
  fi

  # Install uv as the real user (not root)
  if [[ "$run_user" != "root" ]]; then
    su - "$run_user" -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi

  # Add to PATH for current session
  export PATH="${run_home}/.local/bin:$PATH"

  # Verify installation
  if [[ -x "${run_home}/.local/bin/uv" ]]; then
    local uv_version
    uv_version=$("${run_home}/.local/bin/uv" --version 2>/dev/null | head -1)
    log_ok "  uv installed: ${uv_version}"
  else
    log_error "  Failed to install uv"
    return 1
  fi
}

#──────────────────────────────────────────────────────────────────
# LIGHTTPD (Static File Server, backend behind Caddy)
#──────────────────────────────────────────────────────────────────

# Version marker bumped whenever the generated 99-mcapp.conf content
# changes, so upgraded installs actually pick up the new config (see
# configure_lighttpd() below for why this replaced a "contains
# Cache-Control" substring check).
readonly LIGHTTPD_MCAPP_CONF_VERSION=3

install_lighttpd() {
  log_info "Installing lighttpd..."

  # Check if already installed
  if command -v lighttpd &>/dev/null; then
    log_info "  lighttpd already installed"
    configure_lighttpd
    return 0
  fi

  apt-get install -y -qq lighttpd

  configure_lighttpd

  log_ok "  lighttpd installed and configured"
}

configure_lighttpd() {
  log_info "  Configuring lighttpd..."

  local conf_file="/etc/lighttpd/conf-available/99-mcapp.conf"
  local main_conf="/etc/lighttpd/lighttpd.conf"
  local version_marker="mcapp-lighttpd-config-version: ${LIGHTTPD_MCAPP_CONF_VERSION}"

  # Migrate old installations: if lighttpd.conf has McApp directives baked in,
  # replace it with the clean template to avoid duplicate module conflicts
  if [[ -f "$main_conf" ]] && grep -q "webapp" "$main_conf" 2>/dev/null; then
    local backup="${main_conf}.bak.$(date +%s)"
    cp "$main_conf" "$backup"

    # Find template: installed copy, local script dir, or download from GitHub
    local tmpl=""
    if [[ -f "${INSTALL_DIR:-}/bootstrap/templates/lighttpd.conf" ]]; then
      tmpl="${INSTALL_DIR}/bootstrap/templates/lighttpd.conf"
    elif [[ -n "${SCRIPT_DIR:-}" && -f "${SCRIPT_DIR}/templates/lighttpd.conf" ]]; then
      tmpl="${SCRIPT_DIR}/templates/lighttpd.conf"
    fi

    if [[ -n "$tmpl" ]]; then
      cp "$tmpl" "$main_conf"
    else
      # Piped mode: download from GitHub
      curl -fsSL "${GITHUB_RAW_BASE}/bootstrap/templates/lighttpd.conf" -o "$main_conf"
    fi

    log_info "  Replaced customized lighttpd.conf (backup: $backup)"
  fi

  # Move the (single) listen socket to 127.0.0.1:8082 in the BASE conf. It MUST
  # live here and not in 99-mcapp.conf: the stock/template lighttpd.conf
  # consumes server.port in `include_shell use-ipv6.pl + server.port` BEFORE it
  # includes conf-enabled/*.conf, so server.port may be defined exactly once,
  # up in the base. A stock Debian base ships `server.port = 80` and is NOT
  # caught by the "webapp" migration above, which is exactly how an install ends
  # up with server.port declared in both files → lighttpd's "Duplicate config
  # variable ... server.port" → lighttpd AND Caddy both fail to start. Runs
  # before the version-marker early-return so it reconciles the base regardless
  # of 99-mcapp.conf state. Idempotent: no-ops once the base already reads 8082.
  if [[ -f "$main_conf" ]]; then
    sed -i -E 's/^(server\.port[[:space:]]*=[[:space:]]*)80([[:space:]]*)$/\18082\2/' "$main_conf"
    if ! grep -qE '^[[:space:]]*server\.bind[[:space:]]*=' "$main_conf"; then
      sed -i -E '/^server\.port[[:space:]]*=/a server.bind                 = "127.0.0.1"' "$main_conf"
    fi
  fi

  # Idempotency: rewrite only when the version marker is missing or stale.
  #
  # Previously this only checked whether the file contained the literal
  # string "Cache-Control" — which meant header-policy updates (e.g. adding
  # manifest.webmanifest/version.html to the no-cache set, or the :80 -> 8082
  # port move below) never reached already-upgraded installs: any config
  # that already had *some* Cache-Control directive was considered "done"
  # forever. A version marker that bumps on every content change fixes that.
  if [[ -f "$conf_file" ]] && grep -qF "$version_marker" "$conf_file" 2>/dev/null; then
    log_info "  lighttpd already configured (version ${LIGHTTPD_MCAPP_CONF_VERSION})"
    return 0
  fi

  # Create McApp-specific config
  # (heredoc is quoted to protect lighttpd's $HTTP[...] syntax from shell
  # expansion — the version marker is interpolated afterwards via sed)
  cat > "$conf_file" << 'EOF'
# McApp SPA rewrite + redirect + API proxy configuration
# Enables Vue.js SPA routing, root redirect, and reverse proxy to FastAPI
#
# mcapp-lighttpd-config-version: __LIGHTTPD_MCAPP_CONF_VERSION__
#
# lighttpd runs as the backend behind Caddy. Caddy owns the public-facing
# ports 80/443 (see install_caddy()/configure_caddy() below and
# bootstrap/templates/caddy/Caddyfile.mcapp) and passes headers through
# unchanged — Cache-Control policy is owned here, not in Caddy.

server.modules += ("mod_rewrite", "mod_redirect", "mod_proxy", "mod_setenv")

# NOTE: the listen socket (server.bind = "127.0.0.1", server.port = 8082 — put
# lighttpd behind Caddy, which owns the public :80/:443) is declared in the
# BASE lighttpd.conf, NOT here. lighttpd forbids re-declaring server.port, and
# the base consumes it (include_shell use-ipv6.pl + server.port) before it
# includes this file — declaring it again here fails the whole config with
# "Duplicate config variable ... server.port". port 8081 is reserved by
# mcapp-ble.service's uvicorn — do not reuse it.

# Enable streaming for SSE (Server-Sent Events) — prevents buffering
server.stream-response-body = 2

# Redirect root to webapp
$HTTP["url"] == "/" {
    url.redirect = ("^/$" => "/webapp/")
}

# SPA rewrite for Vue.js router (HTML5 history mode)
$HTTP["url"] =~ "^/webapp/" {
    url.rewrite-if-not-file = (
        "^/webapp/(.*)$" => "/webapp/index.html"
    )
}

# index.html, sw.js, manifest.webmanifest, version.html must always
# revalidate so new hashed bundles / PWA manifest / version marker are
# picked up on deploy
$HTTP["url"] =~ "/webapp/(index\.html|sw\.js|manifest\.webmanifest|version\.html)$" {
    setenv.add-response-header += ( "Cache-Control" => "no-cache" )
}
# content-hashed assets are safe to cache hard
$HTTP["url"] =~ "/webapp/assets/" {
    setenv.add-response-header += ( "Cache-Control" => "public, max-age=31536000, immutable" )
}

# Reverse proxy: SSE event stream + REST API → FastAPI on port 2981
$HTTP["url"] =~ "^/events" {
    proxy.server = ("" => (("host" => "127.0.0.1", "port" => 2981)))
}

$HTTP["url"] =~ "^/api/" {
    proxy.server = ("" => (("host" => "127.0.0.1", "port" => 2981)))
}

$HTTP["url"] =~ "^/health" {
    proxy.server = ("" => (("host" => "127.0.0.1", "port" => 2981)))
}
EOF

  sed -i "s/__LIGHTTPD_MCAPP_CONF_VERSION__/${LIGHTTPD_MCAPP_CONF_VERSION}/" "$conf_file"

  # Enable the config (idempotent)
  if [[ ! -L /etc/lighttpd/conf-enabled/99-mcapp.conf ]]; then
    ln -sf "$conf_file" /etc/lighttpd/conf-enabled/99-mcapp.conf
  fi

  # Test config, then FULL restart (not reload).
  #
  # We only reach here when the config was actually (re)written — the version
  # marker check above early-returns on no-op runs, preserving idempotency.
  # A restart (not reload) is REQUIRED: this run changes server.bind/server.port
  # (moving lighttpd from :80 to 127.0.0.1:8082), and lighttpd does NOT rebind
  # its listen sockets on `reload` — only a restart re-opens them on the new
  # address. The old `reload || restart` was doubly wrong: reload returns 0 so
  # the restart fallback never fired, leaving lighttpd stuck on :80 and Caddy
  # unable to bind it. Freeing :80 here (before configure_caddy runs, next in
  # install_packages) is what lets Caddy take the port on the first run.
  if lighttpd -t -f /etc/lighttpd/lighttpd.conf 2>/dev/null; then
    if systemctl restart lighttpd 2>/dev/null; then
      log_ok "  lighttpd restarted (now bound to 127.0.0.1:8082)"
    else
      log_error "  lighttpd failed to restart — check: systemctl status lighttpd"
    fi
  else
    log_warn "  lighttpd config test failed — check $main_conf and $conf_file for conflicts"
  fi
}

#──────────────────────────────────────────────────────────────────
# CADDY (TLS-terminating front door — LAN-HTTPS default)
#──────────────────────────────────────────────────────────────────

# Version marker bumped whenever the generated Caddyfile content changes,
# so upgraded installs actually pick up the new config (same scheme as
# LIGHTTPD_MCAPP_CONF_VERSION above). Keep this number in sync with the
# "mcapp-caddy-config-version:" comment in
# bootstrap/templates/caddy/Caddyfile.mcapp.
readonly CADDY_CONFIG_VERSION=2

install_caddy() {
  log_info "Installing Caddy..."

  # The Caddyfile template uses a modern post-quantum TLS curve
  # (x25519mlkem768) that needs Caddy >= 2.9. Debian's packaged caddy (trixie
  # ships 2.6.2) is too old — `caddy validate` rejects the curve and Caddy
  # never starts. Install from Caddy's official apt repo to get a current
  # release (matches rpizero's 2.11.x). Skip if a new-enough caddy is present.
  local caddy_minor=0
  if command -v caddy &>/dev/null; then
    caddy_minor=$(caddy version 2>/dev/null | grep -oE '2\.[0-9]+' | head -1 | cut -d. -f2)
    caddy_minor=${caddy_minor:-0}
  fi

  if command -v caddy &>/dev/null && [[ "$caddy_minor" -ge 9 ]]; then
    log_info "  Caddy already installed ($(caddy version 2>/dev/null | head -1))"
  else
    if command -v caddy &>/dev/null; then
      log_info "  Caddy $(caddy version 2>/dev/null | head -1) too old (need >= 2.9) — installing current from official repo"
    fi
    if [[ ! -f /etc/apt/sources.list.d/caddy-stable.list ]]; then
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq
    fi
    # --force-confold: keep our already-written /etc/caddy/Caddyfile on upgrade
    apt-get install -y -qq -o Dpkg::Options::="--force-confold" caddy
    log_ok "  Caddy installed ($(caddy version 2>/dev/null | head -1))"
  fi

  configure_caddy
}

configure_caddy() {
  log_info "  Configuring Caddy..."

  if ! command -v caddy &>/dev/null; then
    log_warn "  caddy binary not found — skipping Caddy configuration"
    return 0
  fi

  local caddy_conf="/etc/caddy/Caddyfile"
  local version_marker="mcapp-caddy-config-version: ${CADDY_CONFIG_VERSION}"

  mkdir -p /etc/caddy

  # If ssl-tunnel-setup.sh has already claimed Caddy for public-domain TLS
  # (it sets TLS_ENABLED=true in config.json via update_mcapp_config() and
  # overwrites this same /etc/caddy/Caddyfile path with a provider-specific
  # template that carries no version marker), do NOT treat that as "stale"
  # and rewrite it back to the LAN-HTTPS default here. Without this guard,
  # every routine upgrade run (e.g. via mcapp-update) would silently revert
  # a working public TLS setup back to LAN-only on each bootstrap run.
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local tls_enabled
    tls_enabled=$(jq -r '.TLS_ENABLED // false' "$CONFIG_FILE" 2>/dev/null)
    if [[ "$tls_enabled" == "true" ]]; then
      log_info "  ssl-tunnel-setup.sh owns Caddy (TLS_ENABLED in config.json) — leaving Caddyfile as-is"
      return 0
    fi
  fi

  # Idempotency via version marker (same scheme as configure_lighttpd) —
  # rewrite only when the marker is missing or stale, skip otherwise. This
  # also means ssl-tunnel-setup.sh's provider Caddyfiles (which carry no
  # such marker) are never silently overwritten by a plain upgrade run —
  # only an explicit `caddy validate` failure or removal brings this
  # default back, see remove_tls() in ssl-tunnel-setup.sh.
  local rewrote=false
  if [[ -f "$caddy_conf" ]] && grep -qF "$version_marker" "$caddy_conf" 2>/dev/null; then
    log_info "  Caddyfile already up to date (version ${CADDY_CONFIG_VERSION})"
  else
    # Find template: installed copy, local script dir, or download from GitHub
    local tmpl=""
    if [[ -f "${INSTALL_DIR:-}/bootstrap/templates/caddy/Caddyfile.mcapp" ]]; then
      tmpl="${INSTALL_DIR}/bootstrap/templates/caddy/Caddyfile.mcapp"
    elif [[ -n "${SCRIPT_DIR:-}" && -f "${SCRIPT_DIR}/templates/caddy/Caddyfile.mcapp" ]]; then
      tmpl="${SCRIPT_DIR}/templates/caddy/Caddyfile.mcapp"
    fi

    if [[ -n "$tmpl" ]]; then
      cp "$tmpl" "$caddy_conf"
    else
      # Piped mode: download from GitHub
      curl -fsSL "${GITHUB_RAW_BASE}/bootstrap/templates/caddy/Caddyfile.mcapp" -o "$caddy_conf"
    fi

    rewrote=true
    log_info "  Caddyfile written to ${caddy_conf} (version ${CADDY_CONFIG_VERSION})"
  fi

  # Validate before (re)starting — a bad config must never take down a
  # previously-working Caddy instance (mirrors the lighttpd -t guard above)
  if ! caddy validate --config "$caddy_conf" --adapter caddyfile &>/dev/null; then
    log_warn "  Caddy config validation failed — leaving caddy service as-is"
    log_warn "  Run: caddy validate --config ${caddy_conf} --adapter caddyfile"
    return 0
  fi

  # enable is idempotent — always ensure Caddy starts on boot
  systemctl enable caddy &>/dev/null || true

  # Restart when the config changed OR when Caddy isn't currently up. The
  # second clause matters on a RECOVERY re-run: if a previous run wrote a valid
  # Caddyfile but Caddy failed to start (e.g. it was an older build that
  # rejected the config, now upgraded), the marker already matches so
  # rewrote=false — without the is-active guard we'd validate happily and leave
  # a down service down.
  if [[ "$rewrote" == "true" ]] || ! systemctl is-active --quiet caddy; then
    # FULL restart (not reload) and CHECK the result. Ordering: this runs
    # AFTER configure_lighttpd() (earlier in install_packages) has already
    # done its full restart to free :80, so Caddy can bind :80/:443 here on
    # the very first run — no second bootstrap pass needed. Restart (not
    # reload-first) so a fresh apt-installed Caddy still holding :80 with its
    # default Caddyfile is fully replaced.
    if systemctl restart caddy 2>/dev/null && systemctl is-active --quiet caddy; then
      log_ok "  Caddy (re)started — serving :80 + :443"
    else
      log_error "  Caddy failed to start / bind :80/:443 (is another server on :80?)"
      log_error "  Check: systemctl status caddy ; journalctl -u caddy -n 30"
      journalctl -u caddy --no-pager -n 20 2>/dev/null || true
      return 1
    fi
  else
    log_info "  Caddy config unchanged and already running — not restarting (idempotent no-op)"
  fi

  # Publish Caddy's internal-CA root cert to the path Caddyfile.mcapp serves
  # at /root.crt. Runs every bootstrap (cheap insurance) — see helper.
  publish_caddy_root_crt

  log_ok "  Caddy configured"
}

# Resolve the hostname Caddy actually terminates TLS for. Used by the HTTPS
# health probe and the CA-warmup nudge so both target the SNI Caddy can answer
# — Caddy has NO certificate for "localhost", so probing localhost:443 fails the
# handshake permanently. LAN default: the :443 site is a literal name in
# /etc/caddy/Caddyfile (from Caddyfile.mcapp). Public-TLS Caddyfiles
# (ssl-tunnel-setup.sh) instead use a `{$TLS_HOSTNAME}` env placeholder with no
# literal :443, so fall back to the TLS_HOSTNAME recorded in config.json, then
# to $(hostname).local.
caddy_site() {
  local site=""
  site=$(awk -F: '/^[A-Za-z0-9._-]+:443[[:space:]]*\{/{print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null)
  local cfg="${CONFIG_FILE:-/etc/mcapp/config.json}"
  if [[ -z "$site" && -f "$cfg" ]] && command -v jq &>/dev/null; then
    site=$(jq -r '.TLS_HOSTNAME // ""' "$cfg" 2>/dev/null)
  fi
  echo "${site:-$(hostname).local}"
}

# Copy Caddy's `tls internal` CA root certificate to /var/lib/caddy/root.crt,
# which Caddyfile.mcapp serves at GET /root.crt so LAN clients can install and
# trust it. Caddy writes the CA lazily under its data dir on first internal-TLS
# use, so we nudge it with an HTTPS request, poll for the source, then copy.
# Idempotent: refreshes the served copy on every run. Never hard-fails — if the
# CA isn't there yet it warns; the next run / health check picks it up.
publish_caddy_root_crt() {
  local ca_dst="/var/lib/caddy/root.crt"
  local ca_src="/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"

  # Nudge Caddy into provisioning the internal CA + issuing the site leaf (both
  # lazy on first TLS use). Must target the site Caddy actually serves (see
  # caddy_site) — it has no cert for "localhost", so an https://localhost
  # handshake fails and does not warm the site leaf.
  local site
  site=$(caddy_site)
  curl -skf --resolve "${site}:443:127.0.0.1" --connect-timeout 3 \
    "https://${site}/health" &>/dev/null || true

  local attempts=5
  local i
  for ((i=1; i<=attempts; i++)); do
    if [[ -f "$ca_src" ]]; then
      if install -m 644 "$ca_src" "$ca_dst" 2>/dev/null; then
        log_ok "  Published Caddy internal CA → ${ca_dst} (served at /root.crt)"
      else
        log_warn "  Could not write ${ca_dst}"
      fi
      return 0
    fi
    sleep 2
  done

  # Fallback: the data dir location can vary with the caddy user's HOME/XDG —
  # search for the authorities/local root.crt anywhere under /var/lib/caddy.
  local found
  found=$(find /var/lib/caddy -type f -name root.crt -path '*authorities/local*' 2>/dev/null | head -1)
  if [[ -n "$found" ]]; then
    if install -m 644 "$found" "$ca_dst" 2>/dev/null; then
      log_ok "  Published Caddy internal CA → ${ca_dst} (served at /root.crt)"
    else
      log_warn "  Could not write ${ca_dst}"
    fi
    return 0
  fi

  log_warn "  Caddy internal CA not generated yet — /root.crt will be published on the next run or by the health check"
  return 0
}
