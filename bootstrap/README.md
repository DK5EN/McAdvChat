# McApp Bootstrap

> **Note:** The GitHub repo is `DK5EN/McApp` (legacy name kept for compatibility).
> The project is called **McApp**.

Unified installer and updater for McApp - the MeshCom message proxy for ham radio operators.

> **Installation instructions, CLI options, and service management** are documented in the main [README.md](../README.md). This file covers bootstrap internals and troubleshooting only.

## What Gets Installed

| Component                 | Purpose                                                            |
| ------------------------- | ------------------------------------------------------------------ |
| lighttpd                  | Static file server + reverse proxy for Vue.js webapp and FastAPI   |
| uv                        | Python package manager (creates venv via `uv sync --all-packages`) |
| mcapp.service             | systemd service for the message proxy                              |
| mcapp-ble.service         | systemd service for BLE communication (optional)                   |
| unblock-bluetooth.service | Ensures Bluetooth is unblocked at boot (rfkill fix)                |
| nftables/iptables         | Firewall rules (nftables on Trixie, iptables on Bookworm)          |
| unattended-upgrades       | Automatic security updates (Debian security patches only)          |
| SSH hardening             | Modern ciphers, ed25519-only host key, rate limiting               |
| Locale generation         | en_US.UTF-8 and de_DE.UTF-8                                        |

## CLI Options

```
./mcapp.sh [OPTIONS]

  --check       Dry-run: show what would be updated
  --force       Skip version checks, reinstall everything
  --reconfigure Re-prompt for configuration values
  --fix         Repair mode: reinstall broken components
  --skip        Skip system setup & packages, deploy only
  --converge    Converge system-level state (packages, firewall, web front
                door) to this release's epoch; deploy nothing
  --dev         Install latest development pre-release
  --tag TAG     Install a specific release tag (e.g. v1.5.1)
  --ref REF     Force the bootstrap tree ref (branch or tag), independent
                of the app version (also: MCAPP_BOOTSTRAP_REF)
  --quiet       Minimal output (for cron jobs)
  --version     Show script version and exit
  --help, -h    Show this help message
```

A piped install (`curl … | sudo bash`) pins script, bootstrap libs,
templates, and app to one resolved release tag for the whole run — the same
command on the same tag reproduces the same box later. `--ref` overrides
just the bootstrap tree (libs + templates), independently of the app
version — see [`../doc/version-logic.md`](../doc/version-logic.md) for the
full resolution rules and [`../README.md`](../README.md) for the
install/update one-liners.

## Migrating from Old Installation

If you previously installed McApp using the old scripts (`install_caddy.sh`, `mc-install.sh`, `install_mcapp.sh`), the bootstrap script will automatically detect and migrate your installation.

**What gets migrated:**

- Your existing `config.json` is preserved (new fields added automatically)
- Webapp and Python scripts are updated in place

**What changes:**

- Install directory moves to `~/mcapp/` with uv-managed `.venv/` inside
- systemd service is updated to use `uv run mcapp`
- New system hardening (firewall, tmpfs, SSH, unattended upgrades) is applied

**Note:** The old `~/venv` is preserved (not deleted). You can remove it manually after verifying the migration worked:

```bash
rm -rf ~/venv
```

## System Hardening

The bootstrap applies comprehensive hardening to protect the SD card and secure the Pi:

### SD Card Protection

- tmpfs for `/var/log` (30MB) and `/tmp` (150MB) — RAM-based, no SD writes
- Volatile journal storage for systemd (20MB max, 1 week retention)
- Reduced logrotate retention (2 rotations)
- Removal of unused bloat packages (camera stack, Mesa, Wayland dependencies)
- Disabled unused services (cups, ModemManager, cloud-init, udisks2, serial-getty, etc.)

### SSH Hardening

- Ed25519-only host key
- Modern ciphers (AES-256-GCM, ChaCha20-Poly1305)
- Root login disabled, max 3 auth tries, 30s login grace time
- MOTD scripts disabled for fast login

### IPv6 Disabled

IPv6 is disabled system-wide to eliminate connection timeouts caused by the Happy Eyeballs algorithm when the IPv6 firewall is closed. This fixes mDNS advertisements and improves connection reliability.

### Unattended Upgrades

Automatic Debian security patches are applied daily. No automatic reboot.

### Desktop Image Rejection

The bootstrap detects desktop Pi OS images (which ship with X11/Wayland) and aborts with an error. McApp requires Raspberry Pi OS **Lite** (headless) — desktop images have too many packages and will OOM on Pi Zero 2W (512MB RAM).

## Firewall Rules

The following ports are opened:

| Port | Protocol | Service                                        |
| ---- | -------- | ---------------------------------------------- |
| 22   | TCP      | SSH (rate limited: 6/min external, LAN exempt) |
| 80   | TCP      | HTTP (lighttpd webapp + API reverse proxy)     |
| 1799 | UDP      | MeshCom node communication                     |
| 5353 | UDP      | mDNS (.local, multicast only)                  |

**Internal-only ports (NOT exposed to the network):**

| Port | Protocol | Service                                            |
| ---- | -------- | -------------------------------------------------- |
| 2981 | TCP      | FastAPI SSE/REST (proxied via lighttpd on port 80) |
| 8081 | TCP      | BLE service (localhost only)                       |

**LAN SSH exemption:** Connections from RFC 1918 private IP ranges (192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12) are NOT rate limited.

**Note:** Older firewall configurations (before Feb 2026) exposed ports 2980 and 2981 directly. The bootstrap automatically removes these legacy rules during upgrade.

## Debian Version Support

| Debian        | Python | Firewall | Status         |
| ------------- | ------ | -------- | -------------- |
| Trixie (13)   | 3.13   | nftables | Primary target |
| Bookworm (12) | 3.11   | iptables | Supported      |

The script auto-detects the Debian version and uses appropriate packages and firewall backend.

## Deployment Details

### Release Tarballs

- Downloaded from GitHub Releases API with SHA256 checksum verification
- Existing installation is backed up before extraction (max 3 backups kept)
- `--dev` flag fetches the latest pre-release instead of stable

### Webapp

- Bundled inside the release tarball (preferred)
- Falls back to separate download for backward compatibility with older releases

### Temporary Swap

On Pi Zero 2W (512MB RAM), a temporary 256MB swap file is created in `/var/tmp/` during apt upgrade operations to prevent OOM kills. It is automatically removed after package installation completes.

## Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u mcapp -n 50

# Check config validity
jq '.' /etc/mcapp/config.json

# Check Python venv
~/mcapp/.venv/bin/python -c "import fastapi, aiohttp; print('OK')"
```

### Cannot access web UI

```bash
# Check lighttpd
sudo systemctl status lighttpd

# Check firewall
sudo nft list ruleset    # Trixie
sudo iptables -L -n      # Bookworm

# Check port
ss -tlnp | grep ':80\b'
```

### BLE not working

```bash
# Check Bluetooth service
sudo systemctl status bluetooth

# Check rfkill
rfkill list bluetooth

# Check BLE adapter
bluetoothctl show

# Check BLE service
sudo systemctl status mcapp-ble
sudo journalctl -u mcapp-ble -n 30
```

### View deployment history

The bootstrap script logs deployment events to the systemd journal:

```bash
# View all deployment events
sudo journalctl -u mcapp.service | grep BOOTSTRAP

# View recent deployments with version info
sudo journalctl -u mcapp.service --since "7 days ago" | grep BOOTSTRAP

# Check last deployment
sudo journalctl -u mcapp.service | grep BOOTSTRAP | tail -5
```

Example output:

```
Feb 15 09:30:14 mcapp mcapp[38295]: [BOOTSTRAP] Stopping service for maintenance and deployment
Feb 15 09:30:14 mcapp mcapp[38295]: [BOOTSTRAP] Current version: v1.01.0
Feb 15 09:30:18 mcapp mcapp[38295]: [BOOTSTRAP] Deployment complete - new version: v1.01.1
Feb 15 09:30:18 mcapp mcapp[38295]: [BOOTSTRAP] Upgraded from v1.01.0 to v1.01.1
```

See [DEPLOYMENT_LOGGING.md](DEPLOYMENT_LOGGING.md) for details.

## File Locations

| Path                                            | Purpose                                     |
| ----------------------------------------------- | ------------------------------------------- |
| `/etc/mcapp/config.json`                        | Configuration file                          |
| `~/mcapp/`                                      | McApp package (pyproject.toml + source)     |
| `~/mcapp/.venv/`                                | Python virtual environment (uv-managed)     |
| `/var/www/html/webapp/`                         | Vue.js web application                      |
| `/var/lib/mcapp/`                               | Runtime data (SQLite database)              |
| `/var/log/mcapp/`                               | Application logs                            |
| `/etc/lighttpd/conf-available/99-mcapp.conf`    | lighttpd SPA rewrite + reverse proxy config |
| `/etc/systemd/system/mcapp.service`             | Main service unit file                      |
| `/etc/systemd/system/mcapp-ble.service`         | BLE service unit file                       |
| `/etc/systemd/system/unblock-bluetooth.service` | Bluetooth rfkill fix                        |

## Uninstallation

```bash
# Stop and disable services
sudo systemctl stop mcapp mcapp-ble
sudo systemctl disable mcapp mcapp-ble

# Remove files
sudo rm -rf /etc/mcapp
sudo rm -rf /var/www/html/webapp
sudo rm -rf /var/lib/mcapp
sudo rm -rf ~/mcapp

# Remove systemd services
sudo rm /etc/systemd/system/mcapp.service
sudo rm /etc/systemd/system/mcapp-ble.service
sudo rm /etc/systemd/system/unblock-bluetooth.service
sudo systemctl daemon-reload
```

## License

See the main repository for license information.

## Support

- GitHub Issues: https://github.com/DK5EN/McApp/issues
- MeshCom Community: https://meshcom.org
