#!/bin/bash
# health.sh - Health checks for McApp bootstrap
# Validates services, endpoints, and configuration

#──────────────────────────────────────────────────────────────────
# MAIN HEALTH CHECK
#──────────────────────────────────────────────────────────────────

health_check() {
  local all_passed=true

  echo ""
  log_info "Running health checks..."
  echo "──────────────────────────────────────────────────────────"

  # Check services
  if ! check_service "mcapp"; then all_passed=false; fi
  if ! check_service "lighttpd"; then all_passed=false; fi
  if ! check_service "caddy"; then all_passed=false; fi

  # Check endpoints
  if ! check_webapp_endpoint; then all_passed=false; fi
  if ! check_udp_port; then all_passed=false; fi
  if ! check_sse_endpoint; then all_passed=false; fi
  if ! check_lighttpd_proxy; then all_passed=false; fi

  # Caddy HTTPS probe is advisory only (does NOT flip all_passed) — on first
  # boot Caddy's `tls internal` CA/leaf generation can lag a few seconds, and
  # we don't want that transient state to hard-abort the whole bootstrap.
  check_caddy_https || true

  # Check data
  if ! check_sqlite_db; then all_passed=false; fi

  # Check config
  if ! check_config_valid; then all_passed=false; fi

  # Check venv
  if ! check_venv; then all_passed=false; fi

  # Check versions
  check_versions

  echo "──────────────────────────────────────────────────────────"

  [[ "$all_passed" == "true" ]]
}

#──────────────────────────────────────────────────────────────────
# SERVICE CHECKS
#──────────────────────────────────────────────────────────────────

check_service() {
  local service="$1"

  if systemctl is-active --quiet "$service" 2>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} running\n" "${service}:"
    return 0
  else
    printf "  %-20s ${RED}[FAIL]${NC} not running\n" "${service}:"
    return 1
  fi
}

#──────────────────────────────────────────────────────────────────
# ENDPOINT CHECKS
#──────────────────────────────────────────────────────────────────

check_webapp_endpoint() {
  # Check HTTP via lighttpd
  if curl -fsSL --connect-timeout 5 "http://localhost/webapp/index.html" &>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} HTTP responding\n" "webapp:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} not responding\n" "webapp:"
  return 1
}

check_udp_port() {
  # Retry generously — on first boot the Pi is slow to start the Python process
  local attempts=8
  for ((i=1; i<=attempts; i++)); do
    if ss -uln | grep -q ':1799\b'; then
      printf "  %-20s ${GREEN}[OK]${NC} port listening\n" "udp (meshcom):"
      return 0
    fi
    sleep 3
  done

  printf "  %-20s ${RED}[FAIL]${NC} port not listening\n" "udp (meshcom):"
  return 1
}

check_sse_endpoint() {
  # Retry a few times — SSE server may need seconds to start
  local attempts=5
  for ((i=1; i<=attempts; i++)); do
    if curl -fsSL --connect-timeout 3 "http://localhost:2981/health" &>/dev/null; then
      printf "  %-20s ${GREEN}[OK]${NC} responding\n" "sse (2981):"
      return 0
    fi
    sleep 2
  done

  printf "  %-20s ${RED}[FAIL]${NC} not responding\n" "sse (2981):"
  return 1
}

check_lighttpd_proxy() {
  # Verify lighttpd proxies /api/ and /events to FastAPI on port 2981
  # Uses /health endpoint which exists on both direct and proxied paths.
  # lighttpd is now the backend on 127.0.0.1:8082 (Caddy owns :80/:443),
  # so probe the backend port directly rather than :80.
  if curl -fsSL --connect-timeout 3 "http://127.0.0.1:8082/health" &>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} proxying to FastAPI\n" "lighttpd proxy:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} proxy not working\n" "lighttpd proxy:"
  return 1
}

check_caddy_https() {
  # Verify Caddy terminates TLS on :443 and proxies /health through to FastAPI.
  #
  # Probe the site Caddy ACTUALLY serves — NOT localhost. `tls internal` mints a
  # leaf for the configured site name only, so Caddy has no certificate for
  # "localhost" and https://localhost fails the TLS handshake every time — this
  # is not a CA-timing issue and retrying never helps (it produced a permanent
  # false WARN). caddy_site() (packages.sh) resolves the real site — the
  # Caddyfile :443 name, else config.json TLS_HOSTNAME (public-TLS), else
  # $(hostname).local — and we probe it on the loopback via --resolve so the
  # check is independent of external DNS/mDNS. -k because the LAN default uses a
  # self-signed internal CA the probe may not trust.
  #
  # NOTE: loopback-only — proves Caddy answers HTTPS locally but canNOT detect a
  # firewall-blocked :443 for external clients; that external contract is
  # verified separately by scripts/pwa-smoke.sh <host>.
  #
  # Advisory only (caller uses || true): on a cold boot the internal leaf can
  # take a moment to issue, so retry a few times, then WARN rather than FAIL so
  # a cold start doesn't abort bootstrap.
  local site
  site=$(caddy_site)

  local attempts=5
  for ((i=1; i<=attempts; i++)); do
    if curl -skf --resolve "${site}:443:127.0.0.1" --connect-timeout 3 "https://${site}/health" &>/dev/null; then
      printf "  %-20s ${GREEN}[OK]${NC} HTTPS responding\n" "caddy https:"
      return 0
    fi
    sleep 2
  done

  printf "  %-20s ${YELLOW}[WARN]${NC} Caddy HTTPS not responding on ${site}:443\n" "caddy https:"
  return 1
}

check_sqlite_db() {
  local db_path="/var/lib/mcapp/messages.db"

  if [[ -f "$db_path" ]]; then
    printf "  %-20s ${GREEN}[OK]${NC} exists\n" "sqlite db:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} missing\n" "sqlite db:"
  return 1
}

#──────────────────────────────────────────────────────────────────
# CONFIGURATION CHECK
#──────────────────────────────────────────────────────────────────

check_config_valid() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    printf "  %-20s ${RED}[FAIL]${NC} file missing\n" "config:"
    return 1
  fi

  # Check if valid JSON
  if ! jq '.' "$CONFIG_FILE" &>/dev/null; then
    printf "  %-20s ${RED}[FAIL]${NC} invalid JSON\n" "config:"
    return 1
  fi

  # Check for template values
  if config_has_template_values; then
    printf "  %-20s ${YELLOW}[WARN]${NC} has template values\n" "config:"
    return 1
  fi

  printf "  %-20s ${GREEN}[OK]${NC} valid\n" "config:"
  return 0
}

#──────────────────────────────────────────────────────────────────
# VENV CHECK
#──────────────────────────────────────────────────────────────────

check_venv() {
  local venv_path="${INSTALL_DIR}/.venv"

  if ! venv_is_valid; then
    printf "  %-20s ${RED}[FAIL]${NC} invalid or missing\n" "python venv:"
    return 1
  fi

  # Check if key packages are importable (including BLE service dependencies)
  if ! "${venv_path}/bin/python" -c "import websockets, dbus_next" 2>/dev/null; then
    printf "  %-20s ${YELLOW}[WARN]${NC} missing packages\n" "python venv:"
    return 1
  fi

  local python_version
  python_version=$("${venv_path}/bin/python" --version 2>&1 | cut -d' ' -f2)
  printf "  %-20s ${GREEN}[OK]${NC} Python ${python_version}\n" "python venv:"
  return 0
}

#──────────────────────────────────────────────────────────────────
# VERSION CHECK
#──────────────────────────────────────────────────────────────────

check_versions() {
  local webapp_version
  webapp_version=$(cat "${INSTALL_DIR}/webapp/version.html" 2>/dev/null || echo "unknown")
  printf "  %-20s ${GREEN}[OK]${NC} %s\n" "webapp version:" "$webapp_version"
  printf "  %-20s ${GREEN}[OK]${NC} McApp Bootstrap v%s\n" "bootstrap:" "$SCRIPT_VERSION"

  # Show active slot info if slot layout exists
  if [[ -n "${SLOTS_DIR:-}" ]] && [[ -L "${SLOTS_DIR}/current" ]]; then
    local active_target
    active_target=$(readlink "${SLOTS_DIR}/current")
    printf "  %-20s ${GREEN}[OK]${NC} %s\n" "active slot:" "$active_target"
  fi
  return 0
}

#──────────────────────────────────────────────────────────────────
# NETWORK NAME DETECTION
#──────────────────────────────────────────────────────────────────

# Does this host:scheme pair actually serve McApp, i.e. is it a real access
# point rather than a name that merely resolves?
#
# The answer must be the /health JSON. "Any HTTP response" is not good enough,
# and neither is `curl -f` on its own: when Caddy's front door does not
# recognise a Host header it replies with an EMPTY 200 — no body, no
# content-type. curl -f sees 200 and reports success, so a check built on exit
# status alone happily advertises a URL that renders a blank page in the
# browser. Matching the "status" key in the body is what separates a genuinely
# proxied answer from a name the front door does not serve. See
# scripts/caddy_config_tests.py, which pins exactly that distinction.
#
# Deliberately array-free so it stays runnable under bash 3.2 (macOS) for the
# regression suite; the rest of this installer is bash-4-only by design.
probe_access_point() {
  local host="$1" scheme="$2" body=""
  if [[ "$scheme" == "https" ]]; then
    # LAN HTTPS uses Caddy's internal CA, which is not in the local trust store
    body=$(curl -fsSk --connect-timeout 2 --max-time 4 "https://${host}/health" 2>/dev/null) || return 1
  else
    body=$(curl -fsS --connect-timeout 2 --max-time 4 "http://${host}/health" 2>/dev/null) || return 1
  fi
  [[ "$body" == *'"status"'* ]]
}

# Populates NETWORK_URLS[] and NETWORK_LABELS[] with verified access points.
# Two-stage verification: `getent hosts` (cheap, weeds out names that do not
# resolve at all) and then a real HTTP probe of the candidate — see
# probe_access_point for why name resolution alone was never enough.
# Sets AVAHI_RUNNING to true/false for the mDNS warning.
detect_network_names() {
  NETWORK_URLS=()
  NETWORK_LABELS=()
  AVAHI_RUNNING=false

  local hostname
  hostname=$(hostname -s)

  local ip_addr
  ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}')

  # Associative array for deduplication (requires Bash 4+)
  declare -A seen_hosts

  # Helper: add a URL if the host hasn't been seen yet AND it passes the probe
  _add_url() {
    local host="$1" scheme="$2" label="$3"
    if [[ -z "$host" || -n "${seen_hosts[$host]+x}" ]]; then
      return
    fi
    # Mark as seen before probing, so a candidate that fails here is not
    # retried by a later rule that happens to derive the same name.
    seen_hosts["$host"]=1
    if ! probe_access_point "$host" "$scheme"; then
      return
    fi
    NETWORK_URLS+=("${scheme}://${host}/webapp")
    NETWORK_LABELS+=("$label")
  }

  # 1. TLS hostname (from config.json)
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local tls_enabled tls_hostname
    tls_enabled=$(jq -r '.TLS_ENABLED // false' "$CONFIG_FILE" 2>/dev/null)
    tls_hostname=$(jq -r '.TLS_HOSTNAME // ""' "$CONFIG_FILE" 2>/dev/null)
    if [[ "$tls_enabled" == "true" && -n "$tls_hostname" ]]; then
      _add_url "$tls_hostname" "https" "TLS"
    fi
  fi

  # 2. mDNS (.local via avahi-daemon)
  if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    AVAHI_RUNNING=true
    if getent hosts "${hostname}.local" &>/dev/null; then
      _add_url "${hostname}.local" "http" "mDNS"
    fi
  fi

  # 3. System domain (from dnsdomainname)
  if command -v dnsdomainname &>/dev/null; then
    local sys_domain
    sys_domain=$(dnsdomainname 2>/dev/null)
    if [[ -n "$sys_domain" ]]; then
      local sys_fqdn="${hostname}.${sys_domain}"
      if getent hosts "$sys_fqdn" &>/dev/null; then
        _add_url "$sys_fqdn" "http" "DNS"
      fi
    fi
  fi

  # 4. Fritz!Box (common for German ham radio operators)
  if getent hosts "${hostname}.fritz.box" &>/dev/null; then
    _add_url "${hostname}.fritz.box" "http" "Fritz!Box"
  fi

  # 5. resolv.conf search/domain entries
  if [[ -f /etc/resolv.conf ]]; then
    local domains
    domains=$(awk '/^(search|domain)/ {for(i=2;i<=NF;i++) print $i}' /etc/resolv.conf)
    local domain
    for domain in $domains; do
      local fqdn="${hostname}.${domain}"
      if getent hosts "$fqdn" &>/dev/null; then
        _add_url "$fqdn" "http" "DNS"
      fi
    done
  fi

  # 6. Reverse DNS of own IP
  if [[ -n "$ip_addr" ]]; then
    local rdns_hosts
    rdns_hosts=$(getent hosts "$ip_addr" 2>/dev/null | awk '{for(i=2;i<=NF;i++) print $i}' || true)
    local rdns_host
    for rdns_host in $rdns_hosts; do
      # Skip bare hostnames (no dot) and localhost entries
      if [[ "$rdns_host" == *"."* && "$rdns_host" != "localhost"* ]]; then
        _add_url "$rdns_host" "http" "rDNS"
      fi
    done
  fi

  # 7. IP address — probed like every other candidate. It used to be added
  # unconditionally ("no verification needed"), which is precisely how the
  # summary ended up printing a blank-page URL: before the :80 catch-all in
  # Caddyfile.mcapp v3, a request by IP matched no site and got the empty 200.
  if [[ -n "$ip_addr" ]]; then
    _add_url "$ip_addr" "http" "IP"
  fi

  # Nothing passed. Rather than print an empty Access Points box, fall back to
  # the IP and label it plainly as unverified — this is the honest outcome when
  # Caddy is down or still restarting, and it must not read as a working install.
  if [[ ${#NETWORK_URLS[@]} -eq 0 && -n "$ip_addr" ]]; then
    NETWORK_URLS+=("http://${ip_addr}/webapp")
    NETWORK_LABELS+=("IP, UNVERIFIED")
  fi

  unset -f _add_url
}

#──────────────────────────────────────────────────────────────────
# SUCCESS SUMMARY
#──────────────────────────────────────────────────────────────────

print_success_summary() {
  detect_network_names

  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║            McApp Installation Complete                   ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Access Points:"
  echo "  ─────────────────────────────────────────────────────────"

  local i
  for i in "${!NETWORK_URLS[@]}"; do
    if [[ $i -eq 0 ]]; then
      printf "    Web UI:     %s  (%s)\n" "${NETWORK_URLS[$i]}" "${NETWORK_LABELS[$i]}"
    else
      printf "                %s  (%s)\n" "${NETWORK_URLS[$i]}" "${NETWORK_LABELS[$i]}"
    fi
  done

  if [[ "$AVAHI_RUNNING" != "true" ]]; then
    echo ""
    echo "    Note: avahi-daemon is not running (mDNS/.local disabled)"
  fi

  echo ""
  echo "  Service Management:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    mcapp:      sudo systemctl status|restart mcapp"
  echo "    mcapp-ble:  sudo systemctl status|restart mcapp-ble"
  echo "    Logs:       sudo journalctl -u mcapp -f"
  echo "    Logs BLE:   sudo journalctl -u mcapp-ble -f"
  echo ""
  echo "  Configuration:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    Config:     ${CONFIG_FILE}"
  echo "    Reconfig:   sudo ~/bootstrap/mcapp.sh --reconfigure"
  echo "    Upgrade:    sudo ~/bootstrap/mcapp.sh"
  echo ""

  # Show callsign if configured
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local callsign
    callsign=$(jq -r '.CALL_SIGN // "not set"' "$CONFIG_FILE" 2>/dev/null)
    echo "  Station: ${callsign}"
    echo ""
  fi

  # Reboot recommendation
  if [[ -f /var/run/reboot-required ]]; then
    echo "  ─────────────────────────────────────────────────────────"
    printf "  ${YELLOW}%s${NC}" "Reboot recommended"
    if [[ -f /var/run/reboot-required.pkgs ]]; then
      local pkgs
      pkgs=$(paste -sd', ' /var/run/reboot-required.pkgs)
      printf " (updated: %s)" "$pkgs"
    fi
    echo ""
    echo "    Run: sudo reboot"
    echo ""
  fi

}

#──────────────────────────────────────────────────────────────────
# DIAGNOSTIC COMMANDS (for troubleshooting)
#──────────────────────────────────────────────────────────────────

print_diagnostic_info() {
  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo "  DIAGNOSTIC INFORMATION"
  echo "═══════════════════════════════════════════════════════════"
  echo ""

  echo "System:"
  echo "  Debian:     $(get_debian_codename)"
  echo "  Kernel:     $(uname -r)"
  echo "  Memory:     $(free -h | awk '/^Mem:/ {print $2 " total, " $3 " used"}')"
  echo "  Disk:       $(df -h / | awk 'NR==2 {print $2 " total, " $3 " used (" $5 ")"}')"
  echo ""

  echo "Python Environment:"
  echo "  System:     $(python3 --version 2>&1)"
  local venv_path="${INSTALL_DIR}/.venv"
  if [[ -f "${venv_path}/bin/python" ]]; then
    echo "  Venv:       $(${venv_path}/bin/python --version 2>&1)"
    echo "  Install:    ${INSTALL_DIR}"
    echo "  Packages:"
    "${venv_path}/bin/pip" list 2>/dev/null | grep -E "websockets|dbus-next|timezonefinder|httpx|zstandard" | sed 's/^/    /'
  fi
  echo ""

  echo "Services:"
  for svc in mcapp lighttpd bluetooth avahi-daemon; do
    local status
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
      status="running"
    elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
      status="stopped (enabled)"
    else
      status="disabled"
    fi
    printf "  %-16s %s\n" "${svc}:" "$status"
  done
  echo ""

  echo "Network Ports:"
  echo "  TCP:"
  ss -tln 2>/dev/null | grep -E ':(80|2980|2981)\b' | awk '{print "    " $4}'
  echo "  UDP:"
  ss -uln 2>/dev/null | grep -E ':1799\b' | awk '{print "    " $4}'
  echo ""

  echo "Configuration:"
  if [[ -f "$CONFIG_FILE" ]]; then
    echo "  File: ${CONFIG_FILE}"
    jq '.' "$CONFIG_FILE" 2>/dev/null | head -20 | sed 's/^/  /'
  else
    echo "  Config file not found"
  fi
  echo ""
}
