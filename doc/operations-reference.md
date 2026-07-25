# Operations Reference

Content preserved from CLAUDE.md — deployment, configuration, health checks, firewall, and troubleshooting.

## BLE Testing Modes

| Scenario | BLE Mode | Setup |
|----------|----------|-------|
| Non-BLE features | `disabled` | `export MCAPP_BLE_MODE=disabled` |
| Production / Testing with BLE | `remote` | Deploy BLE service on Pi, point URL to it |

Remote BLE testing:
```bash
# On Pi: Start BLE service
cd ble_service
uvicorn src.main:app --host 0.0.0.0 --port 8081

# On Mac/OrbStack/other: Connect to remote BLE
export MCAPP_BLE_MODE=remote
export MCAPP_BLE_URL=http://pi.local:8081
export MCAPP_BLE_API_KEY=your-secret-key
uv run mcapp
```

## Configuration

Configuration lives in `/etc/mcapp/config.json`:
- `UDP_PORT_list/send`: Port 1799 for MeshCom node
- `SSE_ENABLED/SSE_HOST/SSE_PORT`: SSE/REST API (0.0.0.0:2981, proxied via lighttpd)
- `CALL_SIGN`: Node callsign for command handling
- `LAT/LONG/STAT_NAME`: Location for weather service
- `PRUNE_HOURS`: Chat message retention (default 720h = 30 days)
- `PRUNE_HOURS_POS`: Position data retention (default 192h = 8 days)
- `PRUNE_HOURS_ACK`: ACK retention (default 192h = 8 days)
- `MAX_STORAGE_SIZE_MB`: In-memory store limit

Dev config: `/etc/mcapp/config.dev.json` (auto-selected when `MCAPP_ENV=dev`)

### BLE Configuration

```json
{
  "BLE_MODE": "remote",          // "remote" | "disabled" (local mode removed, use BLE service)
  "BLE_REMOTE_URL": "",          // URL for remote BLE service (e.g., http://pi.local:8081)
  "BLE_API_KEY": "auto-generated",  // API key for remote service authentication
  "BLE_DEVICE_NAME": "",         // Auto-connect device name (e.g., "MC-XXXXXX")
  "BLE_DEVICE_ADDRESS": ""       // Auto-connect device MAC address
}
```

**Migration note:** Local mode (`BLE_MODE="local"`) was removed in v1.01.1. For local BLE hardware access, deploy the standalone BLE service (`ble_service/`) and use `BLE_MODE="remote"` pointing to the service URL.

**BLE API Key:** The bootstrap generates a random 16-char key (using `secrets` module) at install time. Both sides use it: McApp sends it as `X-API-Key` header, the BLE service validates it via `BLE_SERVICE_API_KEY` env var. The BLE service has no hardcoded fallback — if no key is set, it runs unauthenticated (with a startup warning).

**Environment variable overrides** (useful for testing):
- `MCAPP_BLE_MODE` - Override BLE mode without editing config
- `MCAPP_BLE_URL` - Override remote BLE service URL
- `MCAPP_BLE_API_KEY` - Override API key

## GitHub Repository

The GitHub repository is at **`github.com/DK5EN/McApp`**.

## Deployment

Target: Raspberry Pi Zero 2W running as systemd service (`mcapp.service`)

### New Bootstrap System (Recommended)

```bash
# Single command for install, update, or repair
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash
```

The bootstrap script:
1. Auto-detects state (fresh/migrate/upgrade)
2. Prompts for configuration on first install
3. Creates Python venv in `~/mcapp-venv` using uv
4. Configures SD card protection (tmpfs, volatile journal)
5. Sets up firewall (nftables on Trixie, iptables on Bookworm)
6. Disables IPv6 (fixes mDNS timeouts caused by Happy Eyeballs algorithm)
7. Enables and starts systemd service

See `bootstrap/README.md` for full documentation.

### Bootstrap Directory Structure

```
bootstrap/
├── mcapp.sh             # Main entry point (run with sudo)
├── lib/
│   ├── detect.sh        # State detection (fresh/migrate/upgrade)
│   ├── config.sh        # Interactive configuration prompts
│   ├── system.sh        # tmpfs, firewall, journald, hardening
│   ├── packages.sh      # apt + uv package management
│   ├── deploy.sh        # Webapp + Python script deployment
│   └── health.sh        # Health checks and diagnostics
├── templates/
│   ├── config.json.tmpl   # Configuration template
│   ├── mcapp.service      # systemd unit file
│   ├── mcapp-ble.service  # BLE service systemd unit
│   ├── mcapp-update.path   # systemd path trigger for OTA updates
│   ├── mcapp-update.service # systemd oneshot service for update runner
│   ├── nftables.conf      # Firewall rules (ports 22, 80, 1799, 2985)
│   ├── journald.conf      # Volatile journal config
│   ├── caddy/             # TLS reverse proxy templates
│   │   ├── Caddyfile.duckdns.tmpl
│   │   ├── Caddyfile.cloudflare.tmpl
│   │   ├── Caddyfile.desec.tmpl
│   │   └── caddy.service
│   └── cloudflared/       # Cloudflare Tunnel templates
│       ├── config.yml.tmpl
│       └── cloudflared.service
└── README.md            # Installation documentation
```

### Bootstrap CLI Options

```bash
sudo ./mcapp.sh --check       # Dry-run, show what would change
sudo ./mcapp.sh --force       # Force reinstall everything
sudo ./mcapp.sh --fix         # Repair broken installation
sudo ./mcapp.sh --reconfigure # Re-prompt for config values
sudo ./mcapp.sh --quiet       # Minimal output (for cron)
```

### BLE Service Deployment (Optional)

For distributed setups where McApp runs on a different machine than the Bluetooth hardware:

```bash
# On Pi with Bluetooth hardware
cd ~/mcapp/ble_service
uv sync

# Configure API key
export BLE_SERVICE_API_KEY=your-secret-key

# Run directly
uvicorn src.main:app --host 0.0.0.0 --port 8081

# Or install as systemd service
sudo cp mcapp-ble.service /etc/systemd/system/
sudo systemctl edit mcapp-ble  # Add: Environment=BLE_SERVICE_API_KEY=...
sudo systemctl enable --now mcapp-ble
```

The BLE service exposes:
- `GET /api/ble/status` - Connection status
- `GET /api/ble/devices` - Scan for devices
- `POST /api/ble/connect` - Connect to device
- `POST /api/ble/disconnect` - Disconnect
- `POST /api/ble/send` - Send message/command
- `GET /api/ble/notifications` - SSE notification stream
- `GET /health` - Health check

See `ble_service/README.md` for full API documentation.

## TLS Remote Access (Optional)

For internet access with TLS encryption, run the standalone setup script:

```bash
sudo ./scripts/ssl-tunnel-setup.sh
```

This adds Caddy as a TLS reverse proxy with automated Let's Encrypt DNS-01 certificates and DDNS updates. Supports DuckDNS, Cloudflare, deSEC.io, and Cloudflare Tunnel.

**Architecture with TLS:**
```
Browser (HTTPS) → Caddy:443 (TLS) → lighttpd:80 → {
    /webapp/  → static files
    /events   → proxy to FastAPI:2981
    /api/     → proxy to FastAPI:2981
}
```

See `tls-architecture.md` for diagrams and `tls-maintenance-SOP.md` for maintenance procedures.

## Remote Health Check Commands

Commands for checking system health on the Pi via SSH. The Pi locale is German, so output labels may be in German.

**IMPORTANT quoting rule:** When running `python3 -c` over SSH, use single quotes for the Python code and `\"` for strings inside Python. Never use f-strings with dict key access — use `%` formatting instead. Nested double quotes in f-strings break the SSH quoting.

### System overview
```bash
ssh mcapp.local "uptime && echo '---' && free -h && echo '---' && df -h / /tmp /var/log && echo '---' && vcgencmd measure_temp && echo '---' && cat /proc/loadavg"
```

### Service statuses
```bash
ssh mcapp.local "sudo systemctl status mcapp.service --no-pager -l 2>&1 | head -20"
ssh mcapp.local "sudo systemctl status lighttpd.service --no-pager 2>&1 | head -10"
ssh mcapp.local "sudo systemctl status bluetooth.service --no-pager 2>&1 | head -10"
```

### Process memory usage
```bash
ssh mcapp.local "ps aux | grep -E 'mcapp|uv.*run' | grep -v grep"
```

### Database stats (use % formatting, NOT f-strings)
```bash
ssh mcapp.local "python3 -c '
import sqlite3, os
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
print(\"Schema version:\", conn.execute(\"SELECT version FROM schema_version\").fetchone()[0])
for r in conn.execute(\"SELECT type, COUNT(*) as cnt FROM messages GROUP BY type ORDER BY cnt DESC\"):
    print(\"  messages/%s: %d\" % (r[0], r[1]))
print(\"  station_positions: %d\" % conn.execute(\"SELECT COUNT(*) FROM station_positions\").fetchone()[0])
print(\"  signal_log: %d\" % conn.execute(\"SELECT COUNT(*) FROM signal_log\").fetchone()[0])
print(\"  signal_buckets: %d\" % conn.execute(\"SELECT COUNT(*) FROM signal_buckets\").fetchone()[0])
sz = os.path.getsize(\"/var/lib/mcapp/messages.db\")
print(\"DB size: %.2f MB\" % (sz/1024/1024))
conn.close()
'"
```

### Log checks
```bash
# Overnight logs (pruning runs at 04:00)
ssh mcapp.local "sudo journalctl -u mcapp.service --since '2026-02-13 22:00' --until '2026-02-14 06:00' --no-pager | tail -100"

# Today's logs
ssh mcapp.local "sudo journalctl -u mcapp.service --since today --no-pager | tail -150"

# Errors only (journald priority filter — may miss app-level [ERROR] lines)
ssh mcapp.local "sudo journalctl -u mcapp.service --since today -p err --no-pager"

# Grep for app-level errors/warnings
ssh mcapp.local "sudo journalctl -u mcapp.service --since today --no-pager | grep -i 'error\|exception\|traceback\|warning\|WARN'"

# Log volume count
ssh mcapp.local "sudo journalctl -u mcapp.service --since today --no-pager | wc -l"
```

## Update Runner (OTA Deployment)

The update runner (`scripts/update-runner.py`) is a standalone Python HTTP server (stdlib only, no dependencies) that manages OTA deployments and rollbacks from the webapp UI. It runs on **port 2985** and uses a slot-based architecture with 3 independent deployment slots.

**Launch mechanism:** A systemd `.path` trigger watches for `/var/lib/mcapp/update-trigger`. When the frontend calls `POST /api/update/start`, the SSE handler writes an args file and trigger file, systemd detects the trigger and launches `mcapp-update.service`.

**Systemd units** (templates in `bootstrap/templates/`):
- `mcapp-update.path` — Watches for `/var/lib/mcapp/update-trigger`
- `mcapp-update.service` — Oneshot service running `update-runner.py` with 15-minute timeout

**Update runner endpoints (port 2985):**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/stream` | GET | SSE stream — real-time bootstrap output (`phase`, `log`, `health`, `result` events) |
| `/status` | GET | JSON — mode, result, slot info, active slot, can_rollback flag |
| `/slots` | GET | JSON — version, deployed_at, active flags for all 3 slots |

**Main API endpoints (port 2981, proxied via port 80):**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/update/check` | GET | Check GitHub for available releases (5-min cache) |
| `/api/update/start` | POST | Launch update runner (optional body: `{"dev": true}`) |
| `/api/update/rollback` | POST | Launch rollback runner |
| `/api/update/slots` | GET | Get slot metadata and active status |

**Slot architecture on Pi:**
```
~/mcapp-slots/
├── current → symlink to active slot-N
├── slot-0/              # Independent deployment slot
│   ├── .venv/           # Python venv for this slot
│   ├── scripts/         # Includes update-runner.py
│   ├── src/mcapp/       # Application code
│   └── webapp/          # Frontend files + version.html
├── slot-1/
├── slot-2/
└── meta/
    ├── slot-N.json      # {"slot": N, "version": "v1.x.y", "deployed_at": "...", "active": true}
    ├── slot-N.etc.tar.gz # /etc config snapshot
    └── slot-N.db        # SQLite database backup
```

**Update sequence:**
1. **Prepare** — determine active vs target slot
2. **Snapshot** — backup `/etc/mcapp/`, systemd units, lighttpd config, and SQLite DB (WAL-safe online backup)
3. **Bootstrap** — run `bootstrap/mcapp.sh --skip [--dev]` into target slot (15-min timeout)
4. **Activate** — atomic symlink swap to target slot
5. **Health check** — 8 retries × 3s: mcapp service, lighttpd, webapp HTTP, SSE health, lighttpd proxy
6. **Auto-rollback** — on health failure: restore previous slot's symlink, /etc snapshot, and database backup

## Firewall Configuration

McApp uses host-based firewall to protect the Raspberry Pi. The bootstrap script automatically configures:
- **nftables** on Debian Trixie (and newer)
- **iptables** on Debian Bookworm (legacy fallback)

### Allowed Ports

| Port | Protocol | Service | Access |
|------|----------|---------|--------|
| 22 | TCP | SSH | Rate limited (6/min external, LAN exempt) |
| 80 | TCP | lighttpd | All traffic (serves webapp + proxies API) |
| 1799 | UDP | MeshCom | All traffic (LoRa mesh communication) |
| 2985 | TCP | Update Runner | All traffic (OTA update SSE stream) |
| 5353 | UDP | mDNS | Multicast only (224.0.0.251) |

**LAN exemption for SSH:** Connections from RFC 1918 private IP ranges (192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12) are NOT rate limited.

### Internal-Only Ports (Not Exposed)

| Port | Protocol | Service | Why Not Exposed |
|------|----------|---------|-----------------|
| 2981 | TCP | FastAPI SSE/REST | Proxied via lighttpd on port 80 |

**Note:** Older firewall configurations (before Feb 2026) exposed ports 2980 and 2981 directly. These are now internal-only. The bootstrap script automatically removes them during upgrade.

### Silent Drops (Not Logged)

To prevent log spam, both nftables and iptables silently drop common broadcast/multicast traffic:

- Layer 2 broadcast packets
- Multicast packets (224.0.0.0/4, 239.0.0.0/8)
- Global broadcast (255.255.255.255)
- SSDP/UPnP (port 1900)
- NetBIOS (ports 137, 138)
- LLMNR (port 5355)
- High UDP ports (> 30000)
- IGMP protocol

All other unmatched traffic is **rejected** (not dropped) — the catch-all rule uses `reject` to send TCP RST / ICMP port-unreachable so clients fail fast instead of timing out on silent drops.

### Firewall Logs

**Log format:**
- nftables: `[nftables DROP] ` prefix (logged before reject)
- iptables: `[iptables DROP] ` prefix (logged before reject)

**Rate limiting:** 10 rejects per minute are logged (prevents log spam while maintaining visibility)

**View rejected traffic:**
```bash
# Watch firewall drops in real-time
sudo journalctl -kf | grep DROP

# Count drops by source IP (last hour)
sudo journalctl -k --since "1 hour ago" | grep DROP | awk '{print $NF}' | sort | uniq -c | sort -rn | head -20

# Count drops by destination port (last hour)
sudo journalctl -k --since "1 hour ago" | grep DROP | grep -oP 'DPT=\K\d+' | sort | uniq -c | sort -rn | head -20

# Show all firewall logs from today
sudo journalctl -k --since today | grep DROP
```

### Customizing the Firewall

**To allow additional ports (example: custom service on port 8080):**

For **nftables** (edit `/etc/nftables.conf`):
```nft
# Add before the log rule
tcp dport 8080 accept
```

For **iptables** (edit `/etc/iptables/rules.v4`):
```bash
# Add before the log rule
iptables -A INPUT -p tcp --dport 8080 -j ACCEPT
```

**Apply changes:**
```bash
# nftables
sudo systemctl restart nftables

# iptables
sudo iptables-restore < /etc/iptables/rules.v4
```

**To customize LAN exemption ranges** (e.g., for non-standard subnets), edit the IP ranges in both configurations and apply changes.

## Troubleshooting

### Bluetooth Blocked by rfkill

**Symptom:** `bluetoothctl power on` fails with "Failed to set power on: org.bluez.Error.Failed"

**Diagnosis:**
```bash
rfkill list bluetooth
# Shows: Soft blocked: yes
```

**Root cause:** Some Raspberry Pi images ship with `/etc/modprobe.d/rfkill_default.conf` containing `options rfkill default_state=0`, which blocks all radios (Bluetooth, WiFi) at boot.

**Solution:** The bootstrap script automatically installs `unblock-bluetooth.service` which runs `rfkill unblock bluetooth` after the Bluetooth service starts.

**Manual fix (if not using bootstrap):**
```bash
# Create systemd service
cat <<'EOF' | sudo tee /etc/systemd/system/unblock-bluetooth.service
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

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now unblock-bluetooth

# Verify
rfkill list bluetooth  # Should show: Soft blocked: no
```
