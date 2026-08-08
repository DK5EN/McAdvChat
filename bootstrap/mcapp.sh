#!/bin/bash
# mcapp.sh - Unified Installer/Updater for McApp
# Idempotent, self-healing, Trixie-compatible
# One script for: bootstrap, configure, upgrade, repair
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | bash
#   ./mcapp.sh [OPTIONS]
#
# Options:
#   --check       Dry-run: show what would be updated
#   --force       Skip version checks, reinstall everything
#   --reconfigure Re-prompt for configuration values
#   --fix         Repair mode: reinstall broken components
#   --skip        Skip system setup & packages, deploy only
#   --dev         Install latest development pre-release
#   --tag TAG     Install a specific release tag (e.g. v1.5.1)
#   --quiet       Minimal output (for cron jobs)
#   --version     Show script version and exit

set -eo pipefail

#──────────────────────────────────────────────────────────────────
# CONSTANTS
#──────────────────────────────────────────────────────────────────
readonly SCRIPT_VERSION="2.4.0"

# Detect piped mode (curl | bash) — BASH_SOURCE is empty when piped
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  readonly PIPED_MODE=false
else
  readonly SCRIPT_DIR=""
  readonly PIPED_MODE=true
fi

readonly GITHUB_REPO_BRANCH_DEFAULT="development"
GITHUB_RAW_BASE="https://raw.githubusercontent.com/DK5EN/McApp/${GITHUB_REPO_BRANCH_DEFAULT}"

# Re-enable nounset now that BASH_SOURCE detection is done
set -u

# Installation paths
readonly CONFIG_DIR="/etc/mcapp"
readonly CONFIG_FILE="${CONFIG_DIR}/config.json"
readonly WEBAPP_DIR="/var/www/html/webapp"
readonly SCRIPTS_DIR="/usr/local/bin"
readonly SHARE_DIR="/usr/local/share/mcapp"
readonly GITHUB_REPO="DK5EN/McApp"
readonly GITHUB_API_BASE="https://api.github.com/repos/${GITHUB_REPO}"

# User home directory (handles sudo correctly)
# When running with sudo, $HOME is /root, but we want the actual user's home
get_real_home() {
  if [[ -n "${SUDO_USER:-}" ]]; then
    getent passwd "$SUDO_USER" | cut -d: -f6
  else
    echo "${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
  fi
}

# Paths set after we know the real home
VENV_DIR=""
OLD_VENV_DIR=""
INSTALL_DIR=""
SLOTS_DIR=""
META_DIR=""
DEPLOY_SLOT=""

init_paths() {
  local real_home
  real_home=$(get_real_home)
  SLOTS_DIR="${real_home}/mcapp-slots"
  META_DIR="${SLOTS_DIR}/meta"
  # INSTALL_DIR points through the 'current' symlink for service runtime
  INSTALL_DIR="${SLOTS_DIR}/current"
  VENV_DIR="${real_home}/mcapp-venv"
  OLD_VENV_DIR="${real_home}/venv"
}

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

#──────────────────────────────────────────────────────────────────
# GLOBAL FLAGS (set by parse_args)
#──────────────────────────────────────────────────────────────────
DRY_RUN=false
FORCE=false
RECONFIGURE=false
FIX_MODE=false
QUIET=false
DEV_MODE=false
PIN_TAG=""
SKIP_TO_DEPLOY=false

#──────────────────────────────────────────────────────────────────
# LOGGING
#──────────────────────────────────────────────────────────────────
log_info() {
  [[ "$QUIET" == "true" ]] && return
  echo -e "${BLUE}[INFO]${NC} $*"
}

log_ok() {
  [[ "$QUIET" == "true" ]] && return
  echo -e "${GREEN}[OK]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

log_step() {
  [[ "$QUIET" == "true" ]] && return
  echo -e "${GREEN}==>${NC} $*"
}

#──────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
#──────────────────────────────────────────────────────────────────
require_root() {
  if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
  fi
}

command_exists() {
  command -v "$1" &>/dev/null
}

#──────────────────────────────────────────────────────────────────
# SOURCE LIBRARY FILES
#──────────────────────────────────────────────────────────────────
source_libs() {
  local lib_dir

  # If running from bootstrap directory, use local libs
  if [[ -n "$SCRIPT_DIR" && -d "${SCRIPT_DIR}/lib" ]]; then
    lib_dir="${SCRIPT_DIR}/lib"
  # If installed to share dir, use those
  elif [[ -d "${SHARE_DIR}/lib" ]]; then
    lib_dir="${SHARE_DIR}/lib"
  # Piped mode: download from GitHub (always fresh to pick up bootstrap changes)
  elif [[ "$PIPED_MODE" == "true" ]]; then
    lib_dir=$(download_libs)
    # Clean up downloaded libs when script exits
    trap "rm -rf '$lib_dir'" EXIT
  else
    log_error "Cannot find library files"
    exit 1
  fi

  # shellcheck source=lib/detect.sh
  source "${lib_dir}/detect.sh"
  # shellcheck source=lib/config.sh
  source "${lib_dir}/config.sh"
  # shellcheck source=lib/system.sh
  source "${lib_dir}/system.sh"
  # shellcheck source=lib/packages.sh
  source "${lib_dir}/packages.sh"
  # shellcheck source=lib/deploy.sh
  source "${lib_dir}/deploy.sh"
  # shellcheck source=lib/health.sh
  source "${lib_dir}/health.sh"
}

# Download library files from GitHub for piped mode (curl | bash)
# NOTE: Do NOT set EXIT trap here — this runs in a subshell via $(),
# so an EXIT trap would delete the temp dir before the caller can use it.
# Cleanup is handled by the caller in source_libs().
download_libs() {
  local tmp_dir
  tmp_dir=$(mktemp -d)

  local lib_files=("detect.sh" "config.sh" "system.sh" "packages.sh" "deploy.sh" "health.sh")

  log_info "Piped mode detected — downloading bootstrap libraries..." >&2

  for lib in "${lib_files[@]}"; do
    if ! curl -fsSL --connect-timeout 10 \
      "${GITHUB_RAW_BASE}/bootstrap/lib/${lib}" -o "${tmp_dir}/${lib}"; then
      log_error "Failed to download lib/${lib} from ${GITHUB_RAW_BASE}"
      rm -rf "$tmp_dir"
      exit 1
    fi
  done

  echo "$tmp_dir"
}

#──────────────────────────────────────────────────────────────────
# CLI PARSING
#──────────────────────────────────────────────────────────────────
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check)
        DRY_RUN=true
        shift
        ;;
      --force)
        FORCE=true
        shift
        ;;
      --reconfigure)
        RECONFIGURE=true
        shift
        ;;
      --fix)
        FIX_MODE=true
        shift
        ;;
      --skip)
        SKIP_TO_DEPLOY=true
        shift
        ;;
      --dev)
        DEV_MODE=true
        GITHUB_RAW_BASE="https://raw.githubusercontent.com/DK5EN/McApp/development"
        shift
        ;;
      --tag)
        PIN_TAG="$2"
        shift 2
        ;;
      --quiet)
        QUIET=true
        shift
        ;;
      --version)
        echo "mcapp.sh version ${SCRIPT_VERSION}"
        exit 0
        ;;
      --help|-h)
        show_help
        exit 0
        ;;
      *)
        log_error "Unknown option: $1"
        show_help
        exit 1
        ;;
    esac
  done
}

show_help() {
  cat << EOF
mcapp.sh - Unified Installer/Updater for McApp

Usage:
  ./mcapp.sh [OPTIONS]

Options:
  --check       Dry-run: show what would be updated
  --force       Skip version checks, reinstall everything
  --reconfigure Re-prompt for configuration values
  --fix         Repair mode: reinstall broken components
  --skip        Skip system setup & packages, deploy only
  --dev         Install latest development pre-release
  --tag TAG     Install a specific release tag (e.g. v1.5.1, v1.6.0)
  --quiet       Minimal output (for cron jobs)
  --version     Show script version and exit
  --help, -h    Show this help message

Examples:
  # Fresh install or update
  curl -fsSL ${GITHUB_RAW_BASE}/bootstrap/mcapp.sh | sudo bash

  # Check what would be updated
  sudo ./mcapp.sh --check

  # Force reinstall everything
  sudo ./mcapp.sh --force

  # Repair broken installation
  sudo ./mcapp.sh --fix

  # Install latest dev pre-release
  sudo ./mcapp.sh --dev

  # Quick deploy only (skip system setup & packages)
  sudo ./mcapp.sh --skip
  sudo ./mcapp.sh --skip --dev

  # Install a specific version (for bisecting regressions)
  sudo ./mcapp.sh --skip --tag v1.5.1

  # Change configuration
  sudo ./mcapp.sh --reconfigure
EOF
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"

  # Show banner
  if [[ "$QUIET" != "true" ]]; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║           McApp Bootstrap v${SCRIPT_VERSION}                         ║"
    echo "║   MeshCom Message Proxy for Raspberry Pi                 ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo ""
  fi

  require_root

  # Initialize paths that depend on the real user's home
  init_paths

  source_libs

  # Phase 1: Detect current state
  log_step "Detecting system state..."
  local state
  state=$(detect_install_state)
  local debian_codename
  debian_codename=$(get_debian_codename)
  local python_version
  python_version=$(get_python_version)

  # Abort early if desktop image detected (OOM risk on Pi Zero 2W)
  if ! check_desktop_image; then
    exit 1
  fi

  log_info "Debian: ${debian_codename}"
  log_info "Python: ${python_version}"
  log_info "Install state: ${state}"

  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "Dry-run mode - no changes will be made"
    dry_run_report "$state"
    exit 0
  fi

  if [[ "$SKIP_TO_DEPLOY" == "true" ]]; then
    # --skip: jump straight to deploy, service restart, and health check
    if [[ "$state" == "fresh" || "$state" == "incomplete" ]]; then
      log_error "--skip requires an existing installation (state: ${state})"
      exit 1
    fi
    log_info "Skipping system setup and packages (--skip)"

    # --skip is the path the web updater uses (update-runner.py always runs
    # `mcapp.sh --skip`), which otherwise never runs install_packages() and so
    # would never install the Caddy front door or move lighttpd off :80.
    # ensure_web_frontend() is idempotent (steady-state no-op, no restart), so
    # running it here every update is safe and closes that gap.
    #
    # SEEDING CAVEAT: update-runner.py drives each update using the CURRENTLY
    # ACTIVE (pre-update) slot's mcapp.sh (update-runner.py:366-371), not the
    # incoming release's. So this fix only becomes the driver once a release
    # CONTAINING it is the active slot. The FIRST release that carries this
    # change must therefore be seeded by a one-time manual full run
    # (`sudo mcapp.sh` WITHOUT --skip) on each Pi — documented for the
    # maintainer. After that, subsequent web-updater runs self-heal.
    log_step "Ensuring web front end (lighttpd :8082 + Caddy :80/:443)..."
    ensure_web_frontend
  else
    # Phase 1.5: Migration from old installation (if needed)
    if [[ "$state" == "migrate" ]]; then
      log_step "Detected old installation - migrating..."
      migrate_old_installation
      # After migration prep, treat as upgrade
      state="upgrade"
    fi

    # Phase 2: Configuration (only if needed)
    if [[ "$state" == "fresh" || "$state" == "incomplete" || "$RECONFIGURE" == "true" ]]; then
      log_step "Collecting configuration..."
      collect_config "$state"
    else
      log_info "Using existing configuration"
    fi

    # Phase 3: System setup
    log_step "Configuring system..."
    setup_system

    # Phase 4: Package installation
    log_step "Installing packages..."
    install_packages
  fi

  # Phase 5: Application deployment
  log_step "Deploying application..."
  [[ -n "$PIN_TAG" ]] && log_info "Pinning to tag: ${PIN_TAG}"
  deploy_app "$FORCE" "$DEV_MODE" "$PIN_TAG"

  # Phase 6: Service activation
  log_step "Activating services..."
  activate_services

  # Phase 7: Health check
  log_step "Running health checks..."
  if health_check; then
    if [[ "$SKIP_TO_DEPLOY" != "true" ]]; then
      # Only show terminal summary for interactive runs (not update-runner)
      print_success_summary
    fi
  else
    log_error "Health checks failed - check logs above"
    exit 1
  fi
}

#──────────────────────────────────────────────────────────────────
# DRY RUN REPORT
#──────────────────────────────────────────────────────────────────
dry_run_report() {
  local state="$1"

  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo "  DRY RUN REPORT"
  echo "═══════════════════════════════════════════════════════════"
  echo ""

  echo "Current State: ${state}"
  echo ""

  echo "Would perform the following actions:"
  echo ""

  case "$state" in
    fresh)
      echo "  [CONFIG] Prompt for configuration values"
      echo "  [SYSTEM] Configure tmpfs for /var/log and /tmp"
      echo "  [SYSTEM] Configure nftables firewall"
      echo "  [SYSTEM] Configure journald for volatile storage"
      echo "  [SYSTEM] Disable unused services"
      echo "  [PACKAGES] Install uv package manager"
      echo "  [PACKAGES] Install apt packages (jq, curl, screen, etc.)"
      echo "  [PACKAGES] Install and configure lighttpd (backend on 127.0.0.1:8082)"
      echo "  [PACKAGES] Install and configure Caddy (LAN-HTTPS front door on :80/:443)"
      echo "  [DEPLOY] Download release tarball to ~/mcapp"
      echo "  [DEPLOY] Run uv sync --all-packages to install Python dependencies"
      echo "  [DEPLOY] Download and install webapp"
      echo "  [SERVICES] Enable and start mcapp, lighttpd"
      ;;
    incomplete)
      echo "  [CONFIG] Resume configuration prompts"
      echo "  [PACKAGES] Verify/update packages"
      echo "  [DEPLOY] Verify/update application"
      echo "  [SERVICES] Enable and start services"
      ;;
    migrate)
      echo "  [MIGRATE] Detected old installation (/usr/local/bin scripts)"
      echo "  [MIGRATE] Stop mcapp service"
      echo "  [MIGRATE] Download release tarball to ~/mcapp"
      echo "  [MIGRATE] Run uv sync --all-packages for dependencies"
      echo "  [MIGRATE] Update systemd service to use 'uv run mcapp'"
      echo "  [MIGRATE] Add missing config fields"
      echo "  [SYSTEM] Configure tmpfs, firewall, journald"
      echo "  [PACKAGES] Install uv, update dependencies"
      echo "  [SERVICES] Restart with new configuration"
      echo ""
      echo "  Note: Your existing config.json will be preserved."
      echo "  Old files in /usr/local/bin will NOT be deleted."
      ;;
    upgrade)
      check_versions_report
      ;;
  esac

  report_caddy_state

  echo ""
}

#──────────────────────────────────────────────────────────────────
# CADDY / LIGHTTPD PORT-STATE REPORT (for --check dry-run output)
#──────────────────────────────────────────────────────────────────
# Live-inspects the system (not simulated) so --check reflects reality
# regardless of detect_install_state()'s fresh/incomplete/upgrade/migrate
# bucketing above. CADDY_CONFIG_VERSION and LIGHTTPD_MCAPP_CONF_VERSION are
# defined in lib/packages.sh, already sourced by the time this runs.
report_caddy_state() {
  echo "Caddy / lighttpd port state:"

  if command_exists caddy; then
    local caddy_ver
    caddy_ver=$(caddy version 2>/dev/null | head -1)
    echo "  caddy binary:   present (${caddy_ver:-unknown version})"
  else
    echo "  caddy binary:   NOT installed (would run: apt-get install -y caddy)"
  fi

  if systemctl is-enabled --quiet caddy 2>/dev/null; then
    echo "  caddy service:  enabled"
  else
    echo "  caddy service:  NOT enabled (would enable on install)"
  fi

  if systemctl is-active --quiet caddy 2>/dev/null; then
    echo "  caddy service:  running"
  else
    echo "  caddy service:  NOT running (would start on install)"
  fi

  local caddyfile="/etc/caddy/Caddyfile"
  local tls_enabled="false"
  if [[ -f "$CONFIG_FILE" ]] && command_exists jq; then
    tls_enabled=$(jq -r '.TLS_ENABLED // false' "$CONFIG_FILE" 2>/dev/null)
  fi

  # Use the full marker (version AND hostname) so the dry run reports the same
  # verdict configure_caddy() will act on — a renamed box is stale even at the
  # current version, because the rendered :443 site addresses embed the hostname.
  local caddy_marker=""
  if declare -F caddy_config_marker &>/dev/null; then
    caddy_marker=$(caddy_config_marker)
  else
    caddy_marker="mcapp-caddy-config-version: ${CADDY_CONFIG_VERSION:-0}"
  fi

  if [[ -f "$caddyfile" ]] && grep -qF "$caddy_marker" "$caddyfile" 2>/dev/null; then
    echo "  Caddyfile:      up to date (${caddy_marker})"
  elif [[ "$tls_enabled" == "true" ]]; then
    echo "  Caddyfile:      owned by ssl-tunnel-setup.sh (TLS_ENABLED in config.json) — untouched"
  elif [[ -f "$caddyfile" ]]; then
    echo "  Caddyfile:      present but outdated (would rewrite to version ${CADDY_CONFIG_VERSION})"
  else
    echo "  Caddyfile:      missing (would write ${caddyfile})"
  fi

  local lh_conf="/etc/lighttpd/conf-available/99-mcapp.conf"
  if [[ -f "$lh_conf" ]] && grep -qF "mcapp-lighttpd-config-version: ${LIGHTTPD_MCAPP_CONF_VERSION:-0}" "$lh_conf" 2>/dev/null; then
    echo "  lighttpd conf:  up to date (version ${LIGHTTPD_MCAPP_CONF_VERSION}, bound to 127.0.0.1:8082)"
  else
    echo "  lighttpd conf:  would migrate to 127.0.0.1:8082 (version ${LIGHTTPD_MCAPP_CONF_VERSION})"
  fi

  if ss -tln 2>/dev/null | grep -q ':80\b'; then
    if ss -tlnp 2>/dev/null | grep ':80\b' | grep -q 'caddy'; then
      echo "  port 80 owner:  caddy (expected)"
    else
      echo "  port 80 owner:  something other than caddy — check for a conflict"
    fi
  else
    echo "  port 80 owner:  nothing listening"
  fi
}

# Run main
main "$@"
