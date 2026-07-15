#!/bin/bash
# ssl-tunnel-setup.sh — TLS Remote Access Setup for McApp
#
# Adds Caddy reverse proxy with automated Let's Encrypt TLS (DNS-01 challenge)
# and DDNS updates. Supports DuckDNS, Cloudflare, deSEC.io, and Cloudflare Tunnel.
#
# This is a standalone script — NOT part of bootstrap. Run it once after
# confirming your local McApp setup works.
#
# Usage:
#   sudo ./ssl-tunnel-setup.sh               # Interactive setup
#   sudo ./ssl-tunnel-setup.sh --remove      # Remove TLS and revert to plain HTTP
#   sudo ./ssl-tunnel-setup.sh --status      # Show current TLS status
#
# Requirements:
#   - McApp running and healthy
#   - lighttpd running as the backend on 127.0.0.1:8082 (behind the
#     LAN-HTTPS default Caddyfile.mcapp — this script REPLACES that
#     Caddyfile with a public-domain one, it doesn't run alongside it)
#   - Internet connectivity
#   - Must be run as root (sudo)

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CADDY_BIN="/usr/local/bin/caddy"
readonly CADDY_CONFIG="/etc/caddy/Caddyfile"
readonly CADDY_ENV="/etc/mcapp/caddy.env"
readonly CADDY_SERVICE="/etc/systemd/system/caddy.service"
readonly CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
readonly CLOUDFLARED_CONFIG="/etc/cloudflared/config.yml"
readonly CLOUDFLARED_SERVICE="/etc/systemd/system/cloudflared.service"
readonly MCAPP_CONFIG="/etc/mcapp/config.json"
readonly TEMPLATE_DIR="${SCRIPT_DIR}/bootstrap/templates"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}ℹ${NC}  $*"; }
log_ok()    { echo -e "${GREEN}✔${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
log_error() { echo -e "${RED}✖${NC}  $*"; }

#──────────────────────────────────────────────────────────────────
# PRE-FLIGHT CHECKS
#──────────────────────────────────────────────────────────────────

preflight_checks() {
  log_info "Running pre-flight checks..."

  # Must be root
  if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (sudo)"
    exit 1
  fi

  # Check McApp service
  if systemctl is-active --quiet mcapp.service 2>/dev/null; then
    log_ok "McApp service is running"
  else
    log_warn "McApp service is not running"
    read -rp "Continue anyway? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || exit 1
  fi

  # Check lighttpd — it is now the backend behind Caddy on 127.0.0.1:8082
  # (see bootstrap/templates/caddy/Caddyfile.mcapp, the LAN-HTTPS default
  # installed by bootstrap/lib/packages.sh), not a direct :80 listener.
  if systemctl is-active --quiet lighttpd 2>/dev/null; then
    log_ok "lighttpd is running"
  else
    log_error "lighttpd is not running. Start it first: sudo systemctl start lighttpd"
    exit 1
  fi

  if ss -tlnp 2>/dev/null | grep -q '127\.0\.0\.1:8082'; then
    log_ok "lighttpd backend (127.0.0.1:8082) is listening"
  else
    log_error "lighttpd backend (127.0.0.1:8082) is not listening"
    log_error "Run 'sudo ~/mcapp-slots/current/bootstrap/mcapp.sh' first to migrate lighttpd's port"
    exit 1
  fi

  # Port 80 is expected to already be occupied by the LAN-HTTPS default
  # Caddy — this script's own Caddy install (below) replaces it in place.
  if ss -tlnp 2>/dev/null | grep -q ':80 '; then
    log_info "Port 80 is currently in use (likely the LAN-HTTPS default Caddy) — will be replaced"
  else
    log_warn "Port 80 is not listening — proceeding anyway, this script will bring Caddy up on it"
  fi

  # Check internet
  if curl -sf --max-time 5 https://api.ipify.org >/dev/null 2>&1; then
    local public_ip
    public_ip=$(curl -sf https://api.ipify.org)
    log_ok "Internet connectivity OK (public IP: ${public_ip})"
  else
    log_error "No internet connectivity"
    exit 1
  fi

  # Check for existing installation
  if [[ -f "$CADDY_BIN" ]] || [[ -f "$CLOUDFLARED_BIN" ]]; then
    log_warn "Existing TLS setup detected"
    if systemctl is-active --quiet caddy 2>/dev/null; then
      log_info "  Caddy is running"
    fi
    if systemctl is-active --quiet cloudflared 2>/dev/null; then
      log_info "  cloudflared is running"
    fi
    echo
    read -rp "Reconfigure? This will replace the current setup. [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || exit 0
  fi

  log_ok "Pre-flight checks passed"
  echo
}

#──────────────────────────────────────────────────────────────────
# INTERACTIVE SETUP
#──────────────────────────────────────────────────────────────────

choose_provider() {
  echo "Choose your DNS provider for TLS certificates:"
  echo
  echo "  1) DuckDNS        — Free, easy setup (duckdns.org)"
  echo "  2) Cloudflare     — Own domain on Cloudflare"
  echo "  3) deSEC.io       — Free, privacy-focused (desec.io)"
  echo "  4) Cloudflare Tunnel — Outbound tunnel, no port forwarding needed"
  echo
  read -rp "Choice [1-4]: " choice

  case "$choice" in
    1) PROVIDER="duckdns" ;;
    2) PROVIDER="cloudflare" ;;
    3) PROVIDER="desec" ;;
    4) PROVIDER="cloudflare-tunnel" ;;
    *)
      log_error "Invalid choice"
      exit 1
      ;;
  esac

  log_info "Selected provider: ${PROVIDER}"
  echo
}

collect_duckdns_config() {
  read -rp "DuckDNS subdomain (e.g., 'mystation' for mystation.duckdns.org): " DUCKDNS_SUBDOMAIN
  read -rp "DuckDNS token: " DUCKDNS_TOKEN

  if [[ -z "$DUCKDNS_SUBDOMAIN" || -z "$DUCKDNS_TOKEN" ]]; then
    log_error "Subdomain and token are required"
    exit 1
  fi

  TLS_HOSTNAME="${DUCKDNS_SUBDOMAIN}.duckdns.org"
  log_info "Hostname: ${TLS_HOSTNAME}"
}

collect_cloudflare_config() {
  read -rp "Your domain (e.g., 'example.com'): " CF_ZONE
  read -rp "Subdomain prefix (e.g., 'mcapp' for mcapp.example.com): " CF_SUBDOMAIN
  read -rp "Cloudflare API token (with DNS edit permissions): " CF_API_TOKEN

  if [[ -z "$CF_ZONE" || -z "$CF_SUBDOMAIN" || -z "$CF_API_TOKEN" ]]; then
    log_error "All fields are required"
    exit 1
  fi

  TLS_HOSTNAME="${CF_SUBDOMAIN}.${CF_ZONE}"
  log_info "Hostname: ${TLS_HOSTNAME}"
}

collect_desec_config() {
  read -rp "deSEC subdomain (e.g., 'mystation' for mystation.dedyn.io): " DESEC_SUBDOMAIN
  read -rp "deSEC API token: " DESEC_TOKEN

  if [[ -z "$DESEC_SUBDOMAIN" || -z "$DESEC_TOKEN" ]]; then
    log_error "Subdomain and token are required"
    exit 1
  fi

  TLS_HOSTNAME="${DESEC_SUBDOMAIN}.dedyn.io"
  log_info "Hostname: ${TLS_HOSTNAME}"
}

collect_tunnel_config() {
  echo "Cloudflare Tunnel requires a pre-created tunnel."
  echo "Create one at: https://one.dash.cloudflare.com/ → Networks → Tunnels"
  echo
  read -rp "Tunnel token (from cloudflared tunnel create): " TUNNEL_TOKEN
  read -rp "Hostname (e.g., 'mcapp.example.com'): " TLS_HOSTNAME

  if [[ -z "$TUNNEL_TOKEN" || -z "$TLS_HOSTNAME" ]]; then
    log_error "Tunnel token and hostname are required"
    exit 1
  fi
}

#──────────────────────────────────────────────────────────────────
# INSTALL CADDY
#──────────────────────────────────────────────────────────────────

install_caddy() {
  log_info "Installing Caddy with DNS modules..."

  # Determine architecture
  local arch
  arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
  case "$arch" in
    aarch64|arm64) arch="arm64" ;;
    armhf|armv7l)  arch="armv7" ;;
    amd64|x86_64)  arch="amd64" ;;
    *)
      log_error "Unsupported architecture: ${arch}"
      exit 1
      ;;
  esac

  # Build download URL with required modules
  local modules="github.com/caddy-dns/${PROVIDER},github.com/mholt/caddy-dynamicdns"
  local url="https://caddyserver.com/api/download?os=linux&arch=${arch}&p=${modules}"

  log_info "  Downloading Caddy for linux/${arch}..."
  if curl -fsSL "$url" -o /tmp/caddy; then
    chmod +x /tmp/caddy
    mv /tmp/caddy "$CADDY_BIN"
    log_ok "  Caddy installed: $($CADDY_BIN version)"
  else
    log_error "  Failed to download Caddy"
    exit 1
  fi

  # Create caddy user
  if ! id caddy >/dev/null 2>&1; then
    useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy
    log_ok "  Created caddy user"
  fi

  # Create directories
  mkdir -p /etc/caddy /var/lib/caddy /var/log/caddy
  chown caddy:caddy /var/lib/caddy /var/log/caddy
}

#──────────────────────────────────────────────────────────────────
# INSTALL CLOUDFLARED
#──────────────────────────────────────────────────────────────────

install_cloudflared() {
  log_info "Installing cloudflared..."

  local arch
  arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
  case "$arch" in
    aarch64|arm64) arch="arm64" ;;
    armhf|armv7l)  arch="arm" ;;
    amd64|x86_64)  arch="amd64" ;;
    *)
      log_error "Unsupported architecture: ${arch}"
      exit 1
      ;;
  esac

  local url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"

  if curl -fsSL "$url" -o /tmp/cloudflared; then
    chmod +x /tmp/cloudflared
    mv /tmp/cloudflared "$CLOUDFLARED_BIN"
    log_ok "  cloudflared installed: $($CLOUDFLARED_BIN --version)"
  else
    log_error "  Failed to download cloudflared"
    exit 1
  fi

  mkdir -p /etc/cloudflared
}

#──────────────────────────────────────────────────────────────────
# CONFIGURE
#──────────────────────────────────────────────────────────────────

configure_caddy() {
  log_info "Configuring Caddy..."

  # Render Caddyfile from template
  local tmpl="${TEMPLATE_DIR}/caddy/Caddyfile.${PROVIDER}.tmpl"
  if [[ ! -f "$tmpl" ]]; then
    log_error "Template not found: ${tmpl}"
    exit 1
  fi
  cp "$tmpl" "$CADDY_CONFIG"
  log_ok "  Caddyfile written to ${CADDY_CONFIG}"

  # Write environment file with secrets
  mkdir -p /etc/mcapp
  local env_file="$CADDY_ENV"

  case "$PROVIDER" in
    duckdns)
      cat > "$env_file" << EOF
TLS_HOSTNAME=${TLS_HOSTNAME}
DUCKDNS_SUBDOMAIN=${DUCKDNS_SUBDOMAIN}
DUCKDNS_TOKEN=${DUCKDNS_TOKEN}
EOF
      ;;
    cloudflare)
      cat > "$env_file" << EOF
TLS_HOSTNAME=${TLS_HOSTNAME}
CF_ZONE=${CF_ZONE}
CF_SUBDOMAIN=${CF_SUBDOMAIN}
CF_API_TOKEN=${CF_API_TOKEN}
EOF
      ;;
    desec)
      cat > "$env_file" << EOF
TLS_HOSTNAME=${TLS_HOSTNAME}
DESEC_SUBDOMAIN=${DESEC_SUBDOMAIN}
DESEC_TOKEN=${DESEC_TOKEN}
EOF
      ;;
  esac

  chmod 600 "$env_file"
  log_ok "  Secrets stored in ${env_file} (mode 600)"

  # Install systemd service
  cp "${TEMPLATE_DIR}/caddy/caddy.service" "$CADDY_SERVICE"
  systemctl daemon-reload
  log_ok "  Caddy systemd service installed"
}

configure_cloudflared() {
  log_info "Configuring cloudflared..."

  # Install tunnel using the token
  "$CLOUDFLARED_BIN" service install "$TUNNEL_TOKEN"
  log_ok "  cloudflared configured"
}

#──────────────────────────────────────────────────────────────────
# FIREWALL
#──────────────────────────────────────────────────────────────────

update_firewall() {
  log_info "Updating firewall..."

  # Check if nftables is available
  if command -v nft >/dev/null 2>&1; then
    # Add port 443 if not already open
    if ! nft list ruleset 2>/dev/null | grep -q 'tcp dport 443'; then
      nft add rule inet filter input tcp dport 443 accept
      log_ok "  Opened port 443"
    else
      log_info "  Port 443 already open"
    fi

    # Restrict port 2981 to localhost (if currently open externally)
    if nft list ruleset 2>/dev/null | grep -q 'tcp dport 2981 accept'; then
      # Remove the external 2981 rule — it's proxied through lighttpd now
      nft delete rule inet filter input handle "$(nft -a list chain inet filter input | grep 'tcp dport 2981 accept' | awk '{print $NF}')" 2>/dev/null || true
      log_ok "  Restricted port 2981 (proxied via lighttpd)"
    fi

    # Update nftables config file for persistence
    local nft_conf="/etc/nftables.conf"
    if [[ -f "$nft_conf" ]]; then
      if ! grep -q 'tcp dport 443' "$nft_conf"; then
        sed -i '/tcp dport 80 accept/a\    # HTTPS (Caddy TLS reverse proxy)\n    tcp dport 443 accept' "$nft_conf"
        log_ok "  Updated ${nft_conf} with port 443"
      fi
    fi
  elif command -v iptables >/dev/null 2>&1; then
    # Fallback: iptables
    iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || \
      iptables -A INPUT -p tcp --dport 443 -j ACCEPT
    log_ok "  Opened port 443 (iptables)"
  else
    log_warn "  No firewall manager found — ensure port 443 is open"
  fi
}

#──────────────────────────────────────────────────────────────────
# START SERVICES
#──────────────────────────────────────────────────────────────────

start_caddy() {
  log_info "Starting Caddy..."

  # Validate config
  if ! "$CADDY_BIN" validate --config "$CADDY_CONFIG" 2>/dev/null; then
    log_error "Caddyfile validation failed"
    "$CADDY_BIN" validate --config "$CADDY_CONFIG"
    exit 1
  fi

  systemctl enable caddy
  systemctl start caddy

  # Wait for startup
  sleep 3

  if systemctl is-active --quiet caddy; then
    log_ok "Caddy is running"
  else
    log_error "Caddy failed to start"
    journalctl -u caddy --no-pager -n 20
    exit 1
  fi
}

start_cloudflared() {
  log_info "Starting cloudflared..."

  systemctl enable cloudflared
  systemctl start cloudflared

  sleep 3

  if systemctl is-active --quiet cloudflared; then
    log_ok "cloudflared is running"
  else
    log_error "cloudflared failed to start"
    journalctl -u cloudflared --no-pager -n 20
    exit 1
  fi
}

#──────────────────────────────────────────────────────────────────
# HEALTH CHECK
#──────────────────────────────────────────────────────────────────

health_check() {
  log_info "Running health check..."

  # Test HTTPS locally. Probe the real TLS_HOSTNAME via --resolve, NOT localhost:
  # Caddy serves a cert for TLS_HOSTNAME only, so https://localhost fails the SNI
  # handshake (-> 000) and would falsely report "certificate may still be issuing"
  # forever. --resolve pins the name to the loopback so no real DNS is needed.
  local status
  status=$(curl -sk -o /dev/null -w "%{http_code}" \
    --resolve "${TLS_HOSTNAME}:443:127.0.0.1" "https://${TLS_HOSTNAME}/webapp/" 2>/dev/null || echo "000")

  if [[ "$status" == "200" || "$status" == "301" || "$status" == "302" ]]; then
    log_ok "HTTPS responding (HTTP ${status})"
  else
    log_warn "HTTPS returned HTTP ${status} — certificate may still be issuing"
    log_info "  This is normal on first run. Check again in a few minutes."
  fi

  # Show cert info
  if command -v openssl >/dev/null 2>&1; then
    local cert_info
    cert_info=$(echo | openssl s_client -connect localhost:443 -servername "$TLS_HOSTNAME" 2>/dev/null | openssl x509 -noout -dates 2>/dev/null || true)
    if [[ -n "$cert_info" ]]; then
      log_ok "Certificate info:"
      echo "     ${cert_info}" | head -2
    fi
  fi

  echo
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo -e "  ${GREEN}Setup complete!${NC}"
  echo
  echo -e "  Your McApp is now available at:"
  echo -e "  ${BLUE}https://${TLS_HOSTNAME}/webapp/${NC}"
  echo
  echo "  Useful commands:"
  echo "    sudo systemctl status caddy"
  echo "    sudo journalctl -u caddy -f"
  echo "    sudo caddy reload --config ${CADDY_CONFIG}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

#──────────────────────────────────────────────────────────────────
# UPDATE MCAPP CONFIG
#──────────────────────────────────────────────────────────────────

update_mcapp_config() {
  if [[ -f "$MCAPP_CONFIG" ]] && command -v jq >/dev/null 2>&1; then
    local tmp
    tmp=$(mktemp)
    jq --arg provider "$PROVIDER" \
       --arg hostname "$TLS_HOSTNAME" \
       '. + {"TLS_ENABLED": true, "TLS_PROVIDER": $provider, "TLS_HOSTNAME": $hostname}' \
       "$MCAPP_CONFIG" > "$tmp" && mv "$tmp" "$MCAPP_CONFIG"
    log_ok "Updated ${MCAPP_CONFIG} with TLS settings"
  fi
}

#──────────────────────────────────────────────────────────────────
# REMOVE TLS
#──────────────────────────────────────────────────────────────────

remove_tls() {
  log_info "Removing TLS setup..."

  # Stop and disable Caddy
  if systemctl is-active --quiet caddy 2>/dev/null; then
    systemctl stop caddy
    systemctl disable caddy 2>/dev/null || true
    log_ok "Stopped Caddy"
  fi

  # Stop and disable cloudflared
  if systemctl is-active --quiet cloudflared 2>/dev/null; then
    systemctl stop cloudflared
    systemctl disable cloudflared 2>/dev/null || true
    log_ok "Stopped cloudflared"
  fi

  # Remove service files
  rm -f "$CADDY_SERVICE" "$CLOUDFLARED_SERVICE"
  systemctl daemon-reload

  # Update firewall — remove 443
  if command -v nft >/dev/null 2>&1; then
    local handle
    handle=$(nft -a list chain inet filter input 2>/dev/null | grep 'tcp dport 443 accept' | awk '{print $NF}')
    if [[ -n "$handle" ]]; then
      nft delete rule inet filter input handle "$handle" 2>/dev/null || true
      log_ok "Closed port 443"
    fi
  fi

  # Update McApp config
  if [[ -f "$MCAPP_CONFIG" ]] && command -v jq >/dev/null 2>&1; then
    local tmp
    tmp=$(mktemp)
    jq 'del(.TLS_ENABLED, .TLS_PROVIDER, .TLS_HOSTNAME)' "$MCAPP_CONFIG" > "$tmp" && mv "$tmp" "$MCAPP_CONFIG"
    log_ok "Removed TLS settings from ${MCAPP_CONFIG}"
  fi

  log_ok "TLS removed. lighttpd's backend (127.0.0.1:8082) is unaffected."
  log_warn "Nothing is listening on :80/:443 now — re-run mcapp.sh to restore the"
  log_warn "LAN-HTTPS default Caddyfile (bootstrap/templates/caddy/Caddyfile.mcapp)"
  log_warn "and take back ports 80/443:"
  log_warn "  sudo ~/mcapp-slots/current/bootstrap/mcapp.sh"
  log_info "Caddy binary and config left in place — remove manually if desired:"
  log_info "  sudo rm -f ${CADDY_BIN} ${CADDY_CONFIG} ${CADDY_ENV}"
  log_info "  sudo rm -rf /etc/caddy /var/lib/caddy"
}

#──────────────────────────────────────────────────────────────────
# STATUS
#──────────────────────────────────────────────────────────────────

show_status() {
  echo "McApp TLS Status"
  echo "━━━━━━━━━━━━━━━━"

  # Caddy
  if [[ -f "$CADDY_BIN" ]]; then
    echo -e "Caddy binary:   ${GREEN}installed${NC} ($($CADDY_BIN version 2>/dev/null || echo 'unknown'))"
  else
    echo -e "Caddy binary:   ${YELLOW}not installed${NC}"
  fi

  if systemctl is-active --quiet caddy 2>/dev/null; then
    echo -e "Caddy service:  ${GREEN}running${NC}"
  elif systemctl is-enabled --quiet caddy 2>/dev/null; then
    echo -e "Caddy service:  ${YELLOW}enabled but not running${NC}"
  else
    echo -e "Caddy service:  ${YELLOW}not configured${NC}"
  fi

  # cloudflared
  if [[ -f "$CLOUDFLARED_BIN" ]]; then
    echo -e "cloudflared:    ${GREEN}installed${NC}"
  fi

  if systemctl is-active --quiet cloudflared 2>/dev/null; then
    echo -e "CF tunnel:      ${GREEN}running${NC}"
  fi

  # Config
  if [[ -f "$MCAPP_CONFIG" ]] && command -v jq >/dev/null 2>&1; then
    local tls_enabled hostname provider
    tls_enabled=$(jq -r '.TLS_ENABLED // false' "$MCAPP_CONFIG")
    hostname=$(jq -r '.TLS_HOSTNAME // "not set"' "$MCAPP_CONFIG")
    provider=$(jq -r '.TLS_PROVIDER // "not set"' "$MCAPP_CONFIG")
    echo
    echo "TLS enabled:    ${tls_enabled}"
    echo "Provider:       ${provider}"
    echo "Hostname:       ${hostname}"
  fi

  # Cert check
  if [[ -f "$CADDY_BIN" ]] && systemctl is-active --quiet caddy 2>/dev/null; then
    echo
    local cert_dates
    local -a sni_arg=()
    if [[ -f "$MCAPP_CONFIG" ]] && command -v jq >/dev/null 2>&1; then
      local sni_host
      sni_host=$(jq -r '.TLS_HOSTNAME // ""' "$MCAPP_CONFIG" 2>/dev/null)
      [[ -n "$sni_host" ]] && sni_arg=(-servername "$sni_host")
    fi
    # -servername: openssl otherwise sends SNI "localhost", which Caddy has no
    # cert for -> empty output. Present the SNI Caddy serves so cert dates show.
    cert_dates=$(echo | openssl s_client -connect localhost:443 "${sni_arg[@]}" 2>/dev/null | openssl x509 -noout -dates 2>/dev/null || true)
    if [[ -n "$cert_dates" ]]; then
      echo "Certificate:"
      echo "  ${cert_dates}"
    fi
  fi
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────

main() {
  echo
  echo "╔══════════════════════════════════════════╗"
  echo "║  McApp TLS Remote Access Setup           ║"
  echo "╚══════════════════════════════════════════╝"
  echo

  # Handle flags
  case "${1:-}" in
    --remove)
      preflight_checks
      remove_tls
      exit 0
      ;;
    --status)
      show_status
      exit 0
      ;;
    --help|-h)
      echo "Usage: sudo $0 [--remove|--status|--help]"
      exit 0
      ;;
  esac

  preflight_checks
  choose_provider

  # Collect provider-specific config
  case "$PROVIDER" in
    duckdns)   collect_duckdns_config ;;
    cloudflare) collect_cloudflare_config ;;
    desec)     collect_desec_config ;;
    cloudflare-tunnel) collect_tunnel_config ;;
  esac

  echo
  log_info "Configuration summary:"
  log_info "  Provider:  ${PROVIDER}"
  log_info "  Hostname:  ${TLS_HOSTNAME}"
  echo
  read -rp "Proceed with installation? [Y/n] " answer
  [[ "$answer" =~ ^[Nn]$ ]] && exit 0

  echo

  # Install and configure
  if [[ "$PROVIDER" == "cloudflare-tunnel" ]]; then
    install_cloudflared
    configure_cloudflared
    update_firewall
    start_cloudflared
  else
    install_caddy
    configure_caddy
    update_firewall
    start_caddy
  fi

  update_mcapp_config
  health_check
}

main "$@"
