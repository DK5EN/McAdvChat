#!/bin/bash
# system.sh - System configuration for McApp bootstrap
# Handles: tmpfs, firewall, journald, service hardening, SSH, logrotate
# Incorporates all pi-harden.sh functionality for SD card longevity & security

#──────────────────────────────────────────────────────────────────
# MAIN SYSTEM SETUP FUNCTION
#──────────────────────────────────────────────────────────────────

setup_system() {
  # configure_locale moved to install_apt_deps() — runs after apt upgrade
  disable_unused_services
  remove_bloat_packages
  configure_tmpfs
  configure_journald
  configure_logrotate
  configure_firewall
  configure_bluetooth
  disable_ipv6
  configure_unattended_upgrades
  configure_fast_ssh_login
  configure_ssh_hardening
}

#──────────────────────────────────────────────────────────────────
# LOCALE GENERATION
#──────────────────────────────────────────────────────────────────

configure_locale() {
  log_info "Configuring locale..."

  # Ensure locales package is installed
  if ! dpkg -l locales &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq locales
  fi

  local locale_gen="/etc/locale.gen"
  local needs_generate=false

  # Locales to ensure are generated:
  # - en_US.UTF-8: universal fallback
  # - de_DE.UTF-8: common on German systems (SSH forwards LC_ALL from client)
  local -a required_locales=("en_US.UTF-8" "de_DE.UTF-8")

  for loc in "${required_locales[@]}"; do
    # Check if locale is already available
    if locale -a 2>/dev/null | grep -qi "$(echo "$loc" | sed 's/UTF-8/utf8/')"; then
      continue
    fi

    # Uncomment in locale.gen if present but commented out
    if grep -qE "^#\s*${loc}" "$locale_gen" 2>/dev/null; then
      sed -i "s/^#\s*\(${loc}\)/\1/" "$locale_gen"
      needs_generate=true
      log_info "  Enabled ${loc} in locale.gen"
    elif ! grep -q "^${loc}" "$locale_gen" 2>/dev/null; then
      # Not present at all — add it
      echo "${loc} UTF-8" >> "$locale_gen"
      needs_generate=true
      log_info "  Added ${loc} to locale.gen"
    fi
  done

  if [[ "$needs_generate" == "true" ]]; then
    locale-gen
    log_ok "  Locales generated"
  else
    log_info "  Required locales already available"
  fi
}

#──────────────────────────────────────────────────────────────────
# DISABLE UNUSED SERVICES & TIMERS
#──────────────────────────────────────────────────────────────────

disable_unused_services() {
  log_info "Disabling unused services..."

  # Services safe to disable on a headless Pi
  local -a disable_services=(
    "cups"
    "cups-browsed"
    "ModemManager"
    "triggerhappy"
    # cloud-init suite (not needed on local network Pi)
    "cloud-init-local"
    "cloud-init-main"
    "cloud-init-network"
    "cloud-config"
    "cloud-final"
    # SD card wear reduction
    "udisks2"
    "e2scrub_reap"
    # Serial console (headless — no display attached)
    "serial-getty@ttyS0"
  )

  local -a disable_timers=(
    "e2scrub_all.timer"
    "man-db.timer"
  )

  for svc in "${disable_services[@]}"; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
      systemctl disable --now "$svc" 2>/dev/null || true
      log_info "  Disabled: $svc"
    fi
  done

  for tmr in "${disable_timers[@]}"; do
    if systemctl is-enabled --quiet "$tmr" 2>/dev/null; then
      systemctl disable --now "$tmr" 2>/dev/null || true
      log_info "  Disabled timer: $tmr"
    fi
  done

  # DO NOT disable these (required for McApp):
  # - avahi-daemon (mDNS for .local resolution)
  # - bluetooth (BLE for ESP32 communication)
  # - wpa_supplicant (WiFi connectivity)

  log_ok "  Unused services disabled"
}

#──────────────────────────────────────────────────────────────────
# REMOVE BLOAT PACKAGES
#──────────────────────────────────────────────────────────────────

remove_bloat_packages() {
  log_info "Removing unused packages..."

  # Camera stack — not needed on headless Pi, pulls in TensorFlow/Mesa/Wayland bloat
  local -a purge_packages=(
    "rpicam-apps-core"
    "librpicam-app1"
    "libcamera-ipa"
    "libcamera0.6"
    "libcamera0.7"
  )

  local removed=0
  for pkg in "${purge_packages[@]}"; do
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
      log_info "  Removing: $pkg"
      DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "$pkg" 2>/dev/null || true
      removed=$((removed + 1))
    fi
  done

  if [[ $removed -gt 0 ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y -qq 2>/dev/null || true
    log_ok "  Removed $removed bloat package(s) and orphaned dependencies"
  else
    log_ok "  No bloat packages found"
  fi
}

#──────────────────────────────────────────────────────────────────
# TMPFS CONFIGURATION (SD Card Protection)
#──────────────────────────────────────────────────────────────────

configure_tmpfs() {
  log_info "Configuring tmpfs for SD card longevity..."

  local fstab="/etc/fstab"
  local marker="# McApp tmpfs"

  # Check if already configured (also accept pi-harden marker or legacy MCProxy marker)
  if grep -qE "(McApp tmpfs|MCProxy tmpfs|pi-harden)" "$fstab" 2>/dev/null; then
    log_info "  tmpfs already configured in fstab"
  else
    # Backup fstab
    cp "$fstab" "${fstab}.bak.$(date +%Y%m%d)"

    # Add tmpfs entries — nodev,noexec on /var/log for security
    cat >> "$fstab" << EOF

${marker} - begin
tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,noexec,size=30M 0 0
tmpfs /tmp tmpfs defaults,noatime,nosuid,mode=1777,size=150M 0 0
${marker} - end
EOF

    log_ok "  tmpfs entries added to fstab"
  fi

  # Create log directory structure in tmpfs after mount
  mkdir -p /etc/tmpfiles.d
  cat > /etc/tmpfiles.d/mcapp.conf << 'EOF'
# McApp + system log directories (recreated on every boot in tmpfs)
d /var/log/mcapp     0755 root     root     -
d /var/log/lighttpd  0755 www-data www-data -
d /var/log/journal   0755 root     root     -
d /var/log/apt       0755 root     root     -
d /var/log/private   0700 root     root     -
d /var/log/unattended-upgrades 0755 root root -
f /var/log/lastlog             0644 root root -
f /var/log/wtmp                0664 root utmp -
f /var/log/btmp                0600 root utmp -
EOF

  # Try to mount /var/log now if not already tmpfs
  if findmnt -n /var/log 2>/dev/null | grep -q tmpfs; then
    log_info "  /var/log already mounted as tmpfs"
  else
    # Create dirs before mount so services don't break
    systemd-tmpfiles --create /etc/tmpfiles.d/mcapp.conf 2>/dev/null || true
    if mount /var/log 2>/dev/null; then
      log_ok "  Mounted /var/log tmpfs"
    else
      log_warn "  /var/log tmpfs mount deferred to next reboot"
    fi
  fi

  # Mount /tmp if not already mounted
  if ! mountpoint -q /tmp 2>/dev/null; then
    mount -t tmpfs -o defaults,noatime,nosuid,mode=1777,size=150M tmpfs /tmp || true
  fi
}

#──────────────────────────────────────────────────────────────────
# JOURNALD CONFIGURATION (Volatile Storage)
#──────────────────────────────────────────────────────────────────

configure_journald() {
  log_info "Configuring journald for volatile storage..."

  local conf_dir="/etc/systemd/journald.conf.d"
  local conf_file="${conf_dir}/mcapp-volatile.conf"

  mkdir -p "$conf_dir"

  # Check if already configured (ours or pi-harden's)
  if [[ -f "$conf_file" ]] || [[ -f "${conf_dir}/volatile.conf" ]]; then
    log_info "  journald already configured"
    return 0
  fi

  cat > "$conf_file" << 'EOF'
# McApp: Use volatile storage to protect SD card
[Journal]
Storage=volatile
RuntimeMaxUse=20M
RuntimeKeepFree=10M
RuntimeMaxFileSize=5M
MaxRetentionSec=1week
EOF

  log_ok "  journald configured for volatile storage"

  # Restart journald to apply
  systemctl restart systemd-journald || true
}

#──────────────────────────────────────────────────────────────────
# LOGROTATE (Reduce retention for SD card longevity)
#──────────────────────────────────────────────────────────────────

configure_logrotate() {
  log_info "Reducing logrotate retention..."

  if [[ ! -f /etc/logrotate.conf ]]; then
    log_info "  logrotate.conf not found, skipping"
    return 0
  fi

  if grep -qE '^\s*rotate\s+2\b' /etc/logrotate.conf 2>/dev/null; then
    log_info "  logrotate already set to rotate 2"
    return 0
  fi

  sed -i 's/^\s*rotate\s\+[0-9]\+/rotate 2/' /etc/logrotate.conf
  log_ok "  logrotate set to rotate 2"
}

#──────────────────────────────────────────────────────────────────
# FIREWALL CONFIGURATION (nftables)
#──────────────────────────────────────────────────────────────────

configure_firewall() {
  log_info "Configuring firewall..."

  local codename
  codename=$(get_debian_codename)

  # Use nftables for Trixie+, iptables for older
  if [[ "$codename" == "trixie" ]] || [[ "$codename" == "sid" ]]; then
    configure_nftables
  else
    configure_iptables_legacy
  fi
}

configure_nftables() {
  log_info "  Using nftables (Trixie)"

  # Install nftables if not present
  if ! command -v nft &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq nftables
  fi

  local nft_conf="/etc/nftables.conf"
  local marker="# McApp firewall rules"

  # Check if our rules already exist and are up to date
  if grep -q "$marker" "$nft_conf" 2>/dev/null; then
    local needs_update=false

    # Update if ports 2980/2981 are still present (legacy config)
    if grep -q "dport 2980\|dport 2981" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (removing legacy ports 2980/2981)..."
      needs_update=true
    fi

    # Update if LAN SSH exemption is missing
    if ! grep -q "192.168.0.0/16" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (adding LAN SSH exemption)..."
      needs_update=true
    fi

    # Update if log rate limit is still 5/min (old value)
    if grep -q "limit rate 5/minute" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (increasing log rate limit to 10/min)..."
      needs_update=true
    fi

    # Update if port 2985 (update runner) is missing
    if ! grep -q "dport 2985" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (adding update runner port 2985)..."
      needs_update=true
    fi

    # Update if port 443 (Caddy HTTPS front door) is missing — Wave 0.
    # Without this clause, already-marked installs (e.g. mcapp, which has the
    # marker + the 2985 rule) would SKIP the rewrite and never open :443,
    # leaving LAN/iPhone HTTPS clients firewall-dropped even with Caddy up.
    if ! grep -q "dport 443" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (adding Caddy HTTPS port 443)..."
      needs_update=true
    fi

    # Update if AIOps journal cross-shipping port 19532 is missing.
    # Pi↔Pi journal-upload→journal-remote; LAN-scoped, required for off-box
    # log evidence (AIOps AD-8, B-4). Without this, journal-remote is unreachable
    # after any nftables rewrite and shipping silently stops.
    if ! grep -q "dport 19532" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (adding AIOps journal-xship port 19532)..."
      needs_update=true
    fi

    if [[ "$needs_update" == "false" ]]; then
      log_info "  nftables rules already configured"
      return 0
    fi
  fi

  # Backup existing config
  [[ -f "$nft_conf" ]] && cp "$nft_conf" "${nft_conf}.bak.$(date +%Y%m%d)"

  cat > "$nft_conf" << 'EOF'
#!/usr/sbin/nft -f
# McApp firewall rules
# Generated by mcapp.sh bootstrap
#
# Required ports:
#   22/tcp   - SSH (rate limited, LAN exempt)
#   80/tcp   - HTTP (Caddy front door → lighttpd/FastAPI)
#   443/tcp  - HTTPS (Caddy front door — Wave 0)
#   1799/udp - MeshCom node communication
#   5353/udp - mDNS (avahi for .local resolution)
#   2985/tcp - Update runner SSE stream
#   19532/tcp - AIOps journal cross-shipping (Pi↔Pi, LAN only)
#
# Internal only (not exposed):
#   2981/tcp - FastAPI SSE/REST (proxied via Caddy→lighttpd)
#   8082/tcp - lighttpd backend (127.0.0.1 only, behind Caddy)

flush ruleset

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;

    # Allow loopback interface
    iif lo accept

    # Allow established/related connections
    ct state established,related accept

    # Drop invalid packets
    ct state invalid drop

    # SSH - allow local LAN without rate limiting
    ip saddr { 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12 } tcp dport 22 accept

    # SSH - rate limit external connections (6 new per minute)
    tcp dport 22 ct state new limit rate 6/minute accept

    # HTTP (Caddy front door → lighttpd/FastAPI)
    tcp dport 80 accept

    # HTTPS (Caddy HTTPS front door — Wave 0; LAN/iPhone clients hit :443)
    tcp dport 443 accept

    # Update runner SSE stream (frontend-triggered updates)
    tcp dport 2985 accept

    # AIOps journal cross-shipping (Pi↔Pi, LAN only — AIOps AD-8/B-4)
    ip saddr { 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12 } tcp dport 19532 accept

    # MeshCom UDP communication
    udp dport 1799 accept

    # mDNS for .local hostname resolution (avahi-daemon)
    # Restrict to multicast destinations per mDNS protocol spec
    ip daddr 224.0.0.251 udp dport 5353 accept

    # ICMP (ping) - useful for diagnostics
    icmp type echo-request accept

    # Silently drop common broadcast/multicast traffic to prevent log spam

    # Broadcast packets (layer 2 broadcast MAC)
    meta pkttype broadcast drop

    # Multicast packets (layer 2 multicast MAC)
    meta pkttype multicast drop

    # Global broadcast
    ip daddr 255.255.255.255 drop

    # Subnet-directed broadcasts
    ip daddr & 0.0.0.255 == 0.0.0.255 drop

    # SSDP/UPnP device discovery
    udp dport 1900 drop

    # mDNS unicast queries (already allowed multicast mDNS above)
    udp sport 5353 drop
    udp dport 5353 drop

    # Common high UDP ports (gaming, P2P, etc)
    udp dport > 30000 drop

    # IGMP multicast group management
    meta l4proto igmp drop

    # NetBIOS Name Service
    udp dport { 137, 138 } drop

    # LLMNR (Link-Local Multicast Name Resolution - Windows)
    udp dport 5355 drop

    # Common multicast addresses
    ip daddr { 224.0.0.0/4, 239.0.0.0/8 } drop

    # Log remaining dropped packets (limited to prevent log spam)
    limit rate 10/minute log prefix "[nftables DROP] " flags all counter drop
  }

  chain forward {
    type filter hook forward priority 0; policy drop;
  }

  chain output {
    type filter hook output priority 0; policy accept;
  }
}
EOF

  # Enable and start nftables
  systemctl enable nftables
  systemctl restart nftables

  log_ok "  nftables firewall configured and enabled"
}

configure_iptables_legacy() {
  log_info "  Using iptables (Bookworm fallback)"

  # Install iptables-persistent if not present
  if ! dpkg -l iptables-persistent &>/dev/null; then
    # Pre-answer debconf questions to avoid interactive prompt
    echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
    echo iptables-persistent iptables-persistent/autosave_v6 boolean true | debconf-set-selections
    apt-get update -qq
    apt-get install -y -qq iptables-persistent
  fi

  # Flush existing rules
  iptables -F
  iptables -X

  # Default policies
  iptables -P INPUT DROP
  iptables -P FORWARD DROP
  iptables -P OUTPUT ACCEPT

  # Allow loopback
  iptables -A INPUT -i lo -j ACCEPT

  # Allow established connections
  iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

  # SSH - allow local LAN without rate limiting
  iptables -A INPUT -p tcp --dport 22 -s 192.168.0.0/16 -j ACCEPT
  iptables -A INPUT -p tcp --dport 22 -s 10.0.0.0/8 -j ACCEPT
  iptables -A INPUT -p tcp --dport 22 -s 172.16.0.0/12 -j ACCEPT

  # SSH - rate limit external connections (4 per minute)
  iptables -A INPUT -p tcp --dport 22 -m state --state NEW -m recent --set
  iptables -A INPUT -p tcp --dport 22 -m state --state NEW -m recent --update --seconds 60 --hitcount 4 -j DROP
  iptables -A INPUT -p tcp --dport 22 -j ACCEPT

  # HTTP (Caddy front door → lighttpd/FastAPI)
  iptables -A INPUT -p tcp --dport 80 -j ACCEPT

  # HTTPS (Caddy HTTPS front door — Wave 0; LAN/iPhone clients hit :443)
  iptables -A INPUT -p tcp --dport 443 -j ACCEPT

  # Update runner SSE stream (frontend-triggered updates)
  iptables -A INPUT -p tcp --dport 2985 -j ACCEPT

  # MeshCom UDP communication
  iptables -A INPUT -p udp --dport 1799 -j ACCEPT

  # mDNS for .local hostname resolution (restricted to multicast only)
  iptables -A INPUT -p udp -d 224.0.0.251 --dport 5353 -j ACCEPT

  # ICMP (ping)
  iptables -A INPUT -p icmp --icmp-type echo-request -j ACCEPT

  # Silent drops for common broadcast/multicast traffic (prevents log spam)

  # Broadcast packets
  iptables -A INPUT -m addrtype --dst-type BROADCAST -j DROP

  # Common multicast addresses
  iptables -A INPUT -d 224.0.0.0/4 -j DROP
  iptables -A INPUT -d 239.0.0.0/8 -j DROP

  # Global broadcast
  iptables -A INPUT -d 255.255.255.255 -j DROP

  # SSDP/UPnP device discovery
  iptables -A INPUT -p udp --dport 1900 -j DROP

  # NetBIOS name service
  iptables -A INPUT -p udp --dport 137 -j DROP
  iptables -A INPUT -p udp --dport 138 -j DROP

  # LLMNR (Windows name resolution)
  iptables -A INPUT -p udp --dport 5355 -j DROP

  # High UDP ports (gaming, P2P, etc)
  iptables -A INPUT -p udp --dport 30000:65535 -j DROP

  # IGMP multicast management
  iptables -A INPUT -p igmp -j DROP

  # Log remaining drops (rate limited to 10/min to prevent spam)
  iptables -A INPUT -m limit --limit 10/min -j LOG --log-prefix "[iptables DROP] " --log-level 7

  # Save rules
  iptables-save > /etc/iptables/rules.v4

  log_ok "  iptables firewall configured and saved"
}

#──────────────────────────────────────────────────────────────────
# BLUETOOTH RFKILL FIX
#──────────────────────────────────────────────────────────────────

configure_bluetooth() {
  log_info "Configuring Bluetooth rfkill unblock..."

  local service_file="/etc/systemd/system/unblock-bluetooth.service"

  # Check if already configured
  if [[ -f "$service_file" ]]; then
    log_info "  Bluetooth unblock service already configured"
    return 0
  fi

  # Check if rfkill is blocking bluetooth by default
  # This is common on Pi images with /etc/modprobe.d/rfkill_default.conf
  if [[ -f /etc/modprobe.d/rfkill_default.conf ]]; then
    if grep -q "default_state=0" /etc/modprobe.d/rfkill_default.conf 2>/dev/null; then
      log_info "  Detected rfkill default_state=0 (radios blocked at boot)"
    fi
  fi

  # Write the service unit inline (no external template dependency)
  cat > "$service_file" << 'EOF'
# McApp: Unblock Bluetooth at boot
# Some Pi images ship with rfkill default_state=0 which blocks all radios
# This service ensures Bluetooth is unblocked for BLE communication

[Unit]
Description=Unblock Bluetooth for McApp BLE
After=bluetooth.service
Before=mcapp.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill unblock bluetooth
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

  # Enable the service
  systemctl daemon-reload
  systemctl enable unblock-bluetooth

  # Unblock now if currently blocked
  if command -v rfkill &>/dev/null; then
    if rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
      rfkill unblock bluetooth
      log_info "  Bluetooth was blocked, unblocked now"
    fi
  fi

  log_ok "  Bluetooth unblock service installed and enabled"
}

#──────────────────────────────────────────────────────────────────
# DISABLE IPv6
#──────────────────────────────────────────────────────────────────

disable_ipv6() {
  log_info "Disabling IPv6..."

  local sysctl_conf="/etc/sysctl.d/99-disable-ipv6.conf"

  # Check if already configured
  if [[ -f "$sysctl_conf" ]]; then
    if grep -q "net.ipv6.conf.all.disable_ipv6.*=.*1" "$sysctl_conf" 2>/dev/null; then
      log_info "  IPv6 already disabled"
      return 0
    fi
  fi

  # Create sysctl config to disable IPv6
  cat > "$sysctl_conf" << 'EOF'
# McApp: Disable IPv6
# Fixes mDNS advertisements and eliminates Happy Eyeballs timeout
# when IPv6 firewall is closed
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF

  # Apply immediately
  if sysctl -p "$sysctl_conf" &>/dev/null; then
    log_ok "  IPv6 disabled (fixes mDNS timeouts)"
  else
    log_warn "  IPv6 config written, will apply on next reboot"
  fi
}

#──────────────────────────────────────────────────────────────────
# UNATTENDED UPGRADES
#──────────────────────────────────────────────────────────────────

configure_unattended_upgrades() {
  log_info "Configuring unattended security upgrades..."

  # Install if not present
  if ! dpkg -l unattended-upgrades &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq unattended-upgrades
  fi

  local codename
  codename=$(get_debian_codename)

  # Configure which updates to apply
  cat > /etc/apt/apt.conf.d/50unattended-upgrades << EOF
// McApp: Automatic security updates
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${codename},label=Debian-Security";
    "origin=Debian,codename=${codename}-security,label=Debian-Security";
};

// Don't automatically reboot
Unattended-Upgrade::Automatic-Reboot "false";

// Auto-fix interrupted dpkg
Unattended-Upgrade::AutoFixInterruptedDpkg "true";

// Clean up old packages
Unattended-Upgrade::Remove-Unused-Dependencies "true";

// Log to syslog
Unattended-Upgrade::SyslogEnable "true";
EOF

  # Enable automatic updates
  cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

  log_ok "  Unattended upgrades configured (security updates only)"
}

#──────────────────────────────────────────────────────────────────
# FAST SSH LOGIN (disable MOTD delays)
#──────────────────────────────────────────────────────────────────

configure_fast_ssh_login() {
  log_info "Speeding up SSH login..."

  # Disable MOTD scripts (each one adds latency to login)
  if [[ -d /etc/update-motd.d ]]; then
    local disabled=false
    for f in /etc/update-motd.d/*; do
      if [[ -x "$f" ]]; then
        chmod -x "$f"
        disabled=true
      fi
    done
    if [[ "$disabled" == "true" ]]; then
      log_info "  Disabled MOTD scripts"
    else
      log_info "  MOTD scripts already disabled"
    fi
  fi

  # Comment out pam_motd in sshd PAM config (avoids dynamic MOTD generation)
  if [[ -f /etc/pam.d/sshd ]]; then
    if grep -qE '^\s*session.*pam_motd' /etc/pam.d/sshd; then
      sed -i 's/^\(\s*session.*pam_motd\)/#\1/' /etc/pam.d/sshd
      log_info "  Disabled pam_motd in /etc/pam.d/sshd"
    else
      log_info "  pam_motd already disabled"
    fi
  fi

  log_ok "  Fast SSH login configured"
}

#──────────────────────────────────────────────────────────────────
# SSH HARDENING (crypto + access control)
#──────────────────────────────────────────────────────────────────

configure_ssh_hardening() {
  log_info "Configuring SSH hardening..."

  local ssh_conf_dir="/etc/ssh/sshd_config.d"
  local ssh_conf="${ssh_conf_dir}/mcapp-hardening.conf"

  mkdir -p "$ssh_conf_dir"

  # Check if already configured (ours or pi-harden's)
  if [[ -f "$ssh_conf" ]] || [[ -f "${ssh_conf_dir}/10-pi-harden.conf" ]]; then
    log_info "  SSH hardening already configured"
    return 0
  fi

  cat > "$ssh_conf" << 'EOF'
# McApp SSH hardening — crypto + access control
HostKey /etc/ssh/ssh_host_ed25519_key
Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org
UseDNS no
X11Forwarding no
PermitRootLogin no
MaxAuthTries 3
MaxSessions 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF

  # Validate sshd config before restarting
  if sshd -t 2>/dev/null; then
    systemctl restart sshd || true
    log_ok "  SSH hardening applied (ed25519-only, modern ciphers)"
  else
    rm -f "$ssh_conf"
    log_warn "  SSH config invalid, hardening skipped"
  fi
}
