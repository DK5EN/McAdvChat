#!/bin/bash
# config.sh - Interactive configuration & validation for McApp bootstrap
# Handles user prompts, input validation, and config file management

#──────────────────────────────────────────────────────────────────
# VALIDATION PATTERNS
#──────────────────────────────────────────────────────────────────

# Ham radio callsign pattern (e.g., DL0XXX-99, W1ABC-15, OE1ABC-1)
readonly CALLSIGN_PATTERN='^[A-Z]{1,2}[0-9][A-Z]{1,4}(-[0-9]{1,2})?$'

# Latitude range: -90 to 90
readonly LAT_MIN=-90
readonly LAT_MAX=90

# Longitude range: -180 to 180
readonly LON_MIN=-180
readonly LON_MAX=180

# Length of a freshly generated BLE_API_KEY (see generate_ble_api_key()).
# migrate_config() uses this as the "looks weak/placeholder" threshold: any
# existing key shorter than this is rotated, anything at least this long is
# assumed already generated and left alone.
readonly BLE_API_KEY_LENGTH=16

#──────────────────────────────────────────────────────────────────
# INPUT PROMPTS
#──────────────────────────────────────────────────────────────────

# Prompt for input with default value
prompt_with_default() {
  local prompt="$1"
  local default="$2"
  local result

  if [[ -n "$default" ]]; then
    read -rp "[?] ${prompt} [${default}]: " result </dev/tty
    echo "${result:-$default}"
  else
    read -rp "[?] ${prompt}: " result </dev/tty
    echo "$result"
  fi
}

# Prompt for callsign with validation
prompt_callsign() {
  local current="$1"
  local callsign

  while true; do
    callsign=$(prompt_with_default "Enter your callsign (e.g., DL0XXX-99)" "$current")
    callsign="${callsign^^}" # Convert to uppercase

    if validate_callsign "$callsign"; then
      echo "$callsign"
      return 0
    fi

    log_warn "Invalid callsign format. Use format like: DL0XXX-99, W1ABC-15, OE1ABC"
  done
}

# Prompt for MeshCom node address
prompt_node_address() {
  local current="$1"
  local address

  while true; do
    address=$(prompt_with_default "Enter MeshCom node address (hostname or IP)" "$current")

    if validate_node_address "$address"; then
      echo "$address"
      return 0
    fi

    log_warn "Cannot resolve or reach '$address'. Please check the address."
    read -rp "[?] Use anyway? (y/N): " confirm </dev/tty
    if [[ "${confirm,,}" == "y" ]]; then
      echo "$address"
      return 0
    fi
  done
}

# Prompt for latitude
prompt_latitude() {
  local current="$1"
  local lat

  while true; do
    lat=$(prompt_with_default "Enter your latitude (e.g., 48.2082)" "$current")

    if validate_latitude "$lat"; then
      echo "$lat"
      return 0
    fi

    log_warn "Invalid latitude. Must be between ${LAT_MIN} and ${LAT_MAX}."
  done
}

# Prompt for longitude
prompt_longitude() {
  local current="$1"
  local lon

  while true; do
    lon=$(prompt_with_default "Enter your longitude (e.g., 16.3738)" "$current")

    if validate_longitude "$lon"; then
      echo "$lon"
      return 0
    fi

    log_warn "Invalid longitude. Must be between ${LON_MIN} and ${LON_MAX}."
  done
}

# Prompt for station name
prompt_station_name() {
  local current="$1"
  local name

  name=$(prompt_with_default "Enter your city (for weather reports)" "$current")

  # Sanitize: remove special characters, trim whitespace
  name=$(echo "$name" | tr -cd '[:alnum:] _-' | xargs)

  # Default if empty
  [[ -z "$name" ]] && name="McApp Station"

  echo "$name"
}

#──────────────────────────────────────────────────────────────────
# VALIDATION FUNCTIONS
#──────────────────────────────────────────────────────────────────

validate_callsign() {
  local callsign="$1"
  [[ "$callsign" =~ $CALLSIGN_PATTERN ]]
}

validate_node_address() {
  local address="$1"

  # Check if it looks like an IP or hostname
  if [[ -z "$address" ]]; then
    return 1
  fi

  # Try to resolve/ping the address (timeout 2s)
  if ping -c 1 -W 2 "$address" &>/dev/null; then
    return 0
  fi

  # Try DNS resolution only
  if getent hosts "$address" &>/dev/null; then
    return 0
  fi

  return 1
}

validate_latitude() {
  local lat="$1"

  # Check if it's a valid number
  if ! [[ "$lat" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
    return 1
  fi

  # Check range using awk (always available, unlike bc)
  awk "BEGIN { exit ($lat >= $LAT_MIN && $lat <= $LAT_MAX) ? 0 : 1 }"
}

validate_longitude() {
  local lon="$1"

  # Check if it's a valid number
  if ! [[ "$lon" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
    return 1
  fi

  # Check range using awk (always available, unlike bc)
  awk "BEGIN { exit ($lon >= $LON_MIN && $lon <= $LON_MAX) ? 0 : 1 }"
}

#──────────────────────────────────────────────────────────────────
# CONFIG FILE MANAGEMENT
#──────────────────────────────────────────────────────────────────

# Get current config value (or empty if not set/template)
get_config_value() {
  local key="$1"
  local value

  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo ""
    return
  fi

  if ! command -v jq &>/dev/null; then
    echo ""
    return
  fi

  value=$(jq -r ".${key} // \"\"" "$CONFIG_FILE" 2>/dev/null)

  # Return empty if it's a template value
  if [[ "$value" == *"$CONFIG_TEMPLATE_MARKER"* ]]; then
    echo ""
  else
    echo "$value"
  fi
}

# Collect all configuration values
collect_config() {
  # $1 is the detected install state (fresh/incomplete/upgrade/migrate). It is
  # accepted for symmetry with the other phase functions in mcapp.sh but is
  # deliberately unused: the prompt flow is identical for every state, since
  # get_config_value() already returns "" for anything missing or templated.
  # (Was `local state="$1"`, which shellcheck correctly flagged as SC2034.)

  # Get current values (empty if fresh/template)
  local current_callsign
  local current_node
  local current_lat
  local current_lon
  local current_station

  current_callsign=$(get_config_value "CALL_SIGN")
  current_node=$(get_config_value "MESHCOM_IOT_TARGET")
  # Backward compat: try legacy key if new key is empty
  [[ -z "$current_node" ]] && current_node=$(get_config_value "UDP_TARGET")
  current_lat=$(get_config_value "LAT")
  current_lon=$(get_config_value "LONG")
  current_station=$(get_config_value "STAT_NAME")
  current_user_info=$(get_config_value "USER_INFO_TEXT")

  # Loop until user confirms configuration
  while true; do
    echo ""
    log_info "McApp Configuration"
    echo "──────────────────────────────────────────────────────────"

    # Collect values interactively
    local callsign
    local node_address
    local latitude
    local longitude
    local station_name
    local user_info_text

    callsign=$(prompt_callsign "$current_callsign")
    node_address="${callsign}.local"
    latitude=$(prompt_latitude "$current_lat")
    longitude=$(prompt_longitude "$current_lon")
    station_name=$(prompt_station_name "$current_station")
    user_info_text=$(prompt_with_default "Enter user info text (returned by !userinfo)" "${current_user_info:-${callsign} Node}")

    # Resolve node address to IP (|| true prevents set -e from killing the script)
    local node_ip
    node_ip=$(getent hosts "$node_address" 2>/dev/null | awk '{print $1}' || true)

    echo ""
    echo "Configuration summary:"
    echo "  Callsign:       $callsign"
    echo "  MeshCom Node:   $node_address"
    if [[ -n "$node_ip" ]]; then
      echo "  MeshCom Node IP: $node_ip"
    else
      echo "  MeshCom Node IP: (could not resolve)"
    fi
    echo "  Latitude:       $latitude"
    echo "  Longitude:      $longitude"
    echo "  City:           $station_name"
    echo "  User info:      $user_info_text"
    echo ""

    # Warn and offer retry if node cannot be resolved
    if [[ -z "$node_ip" ]]; then
      log_warn "Could not resolve '${node_address}' — the MeshCom node was not found on the network."
      log_warn "McApp needs a reachable MeshCom node for UDP communication."
      echo ""
      read -rp "[?] Re-enter configuration to fix a typo? (Y/n): " retry </dev/tty
      if [[ "${retry,,}" != "n" ]]; then
        # Pre-fill current values for the retry loop
        current_callsign="$callsign"
        current_lat="$latitude"
        current_lon="$longitude"
        current_station="$station_name"
        current_user_info="$user_info_text"
        continue
      fi
      log_info "Continuing with unresolved node address — you can fix this later in ${CONFIG_FILE}"
    fi

    read -rp "[?] Save this configuration? (Y/n): " confirm </dev/tty
    if [[ "${confirm,,}" == "n" ]]; then
      log_warn "Configuration cancelled"
      exit 1
    fi

    break
  done

  # Write config file
  write_config "$callsign" "$node_address" "$latitude" "$longitude" "$station_name" "$user_info_text"

  log_ok "Configuration saved to ${CONFIG_FILE}"
}

# Generate a random BLE API key (BLE_API_KEY_LENGTH chars: upper, lower,
# digits, specials). Shared by write_config() (fresh installs / --reconfigure)
# and migrate_config() (rotating a weak/placeholder key found at upgrade
# time) so there is a single implementation of "what a generated key looks
# like".
generate_ble_api_key() {
  python3 -c "
import secrets, string
alphabet = string.ascii_letters + string.digits + '!@#%^&*_+-='
print(''.join(secrets.choice(alphabet) for _ in range(${BLE_API_KEY_LENGTH})))
"
}

# Write configuration file atomically
write_config() {
  local callsign="$1"
  local node_address="$2"
  local latitude="$3"
  local longitude="$4"
  local station_name="$5"
  local user_info_text="$6"

  # Ensure config directory exists
  mkdir -p "$CONFIG_DIR"

  # Generate random BLE API key
  local ble_api_key
  ble_api_key=$(generate_ble_api_key)

  # Generate config from template or create new
  local tmp_config
  tmp_config=$(mktemp)

  cat > "$tmp_config" << EOF
{
  "CALL_SIGN": "${callsign}",
  "USER_INFO_TEXT": "${user_info_text}",

  "MESHCOM_IOT_TARGET": "${node_address}",

  "LAT": ${latitude},
  "LONG": ${longitude},
  "STAT_NAME": "${station_name}",

  "DB_PATH": "/var/lib/mcapp/messages.db",
  "PRUNE_HOURS": 720,
  "PRUNE_HOURS_POS": 192,
  "PRUNE_HOURS_ACK": 192,

  "BLE_API_KEY": "${ble_api_key}"
}
EOF

  # Validate JSON before writing (skip if jq not yet installed on fresh system)
  if command -v jq >/dev/null 2>&1; then
    if ! jq '.' "$tmp_config" >/dev/null 2>&1; then
      log_error "Generated config is invalid JSON"
      rm -f "$tmp_config"
      return 1
    fi
  elif command -v python3 >/dev/null 2>&1; then
    if ! python3 -c "import json; json.load(open('$tmp_config'))" 2>/dev/null; then
      log_error "Generated config is invalid JSON"
      rm -f "$tmp_config"
      return 1
    fi
  fi

  # Backup existing config if present
  if [[ -f "$CONFIG_FILE" ]]; then
    cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
  fi

  # Atomic move
  mv "$tmp_config" "$CONFIG_FILE"
  chmod 640 "$CONFIG_FILE"

  # Ensure the service user can read the config (script runs as root via sudo)
  local run_user="${SUDO_USER:-root}"
  if [[ "$run_user" != "root" ]]; then
    chown "$run_user:$run_user" "$CONFIG_FILE"
  fi
}

# Migrate config to add missing fields (for upgrades)
migrate_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    return 0
  fi

  log_info "Checking config for missing fields..."

  local updated=false
  local tmp_config
  tmp_config=$(mktemp)

  cp "$CONFIG_FILE" "$tmp_config"

  # Add missing fields with defaults
  local -A defaults=(
    ["PRUNE_HOURS"]=720
    ["PRUNE_HOURS_POS"]=192
    ["PRUNE_HOURS_ACK"]=192
  )

  # `key` MUST be declared local: config.sh is sourced into mcapp.sh's shell,
  # so an undeclared loop variable leaks into every caller's scope and silently
  # clobbers any `key` they were holding across the deploy_app() call.
  local key
  for key in "${!defaults[@]}"; do
    local value="${defaults[$key]}"
    local current
    current=$(jq -r ".${key} // \"__MISSING__\"" "$tmp_config" 2>/dev/null)

    if [[ "$current" == "__MISSING__" ]]; then
      log_info "  Adding missing field: ${key}"

      # Handle different types
      if [[ "$value" == "true" ]] || [[ "$value" == "false" ]] || [[ "$value" =~ ^[0-9]+$ ]]; then
        jq ".${key} = ${value}" "$tmp_config" > "${tmp_config}.new" && mv "${tmp_config}.new" "$tmp_config"
      else
        jq ".${key} = \"${value}\"" "$tmp_config" > "${tmp_config}.new" && mv "${tmp_config}.new" "$tmp_config"
      fi
      updated=true
    fi
  done

  # Rotate a weak/placeholder BLE_API_KEY. This is separate from the
  # missing-fields loop above because it isn't "add if absent" — it must
  # also replace a key that IS present but too weak to have been generated
  # by generate_ble_api_key() (e.g. the hand-written "test-dev-key"
  # placeholder that predates that code and would otherwise survive every
  # upgrade forever, since the loop above only fills in what's missing).
  #
  # Rotation criteria: missing, OR shorter than BLE_API_KEY_LENGTH (covers
  # both "" and any hand-typed short/weak value). A key that is already
  # BLE_API_KEY_LENGTH chars or longer is assumed to already be a generated
  # key and is left untouched — rotating a good key on every deploy would be
  # a silent, recurring BLE outage, which is exactly the failure mode this
  # migration exists to prevent, not reintroduce.
  #
  # Decision on "disabled" vs "" (both make _api_key_valid() treat auth as
  # off): only the literal string "disabled" is exempted from rotation.
  # Nobody ends up with "disabled" by accident — an operator has to type it
  # on purpose to intentionally run the BLE service unauthenticated, and
  # silently rotating that into an enforcing key would break their working
  # setup on the next deploy. An empty string has no such deliberate-choice
  # signal — it's just what an unset/blank field collapses to — so it is
  # treated the same as "missing" and IS rotated.
  local current_ble_key
  current_ble_key=$(jq -r '.BLE_API_KEY // "__MISSING__"' "$tmp_config" 2>/dev/null)

  if [[ "$current_ble_key" != "disabled" ]] && {
       [[ "$current_ble_key" == "__MISSING__" ]] || [[ "${#current_ble_key}" -lt "$BLE_API_KEY_LENGTH" ]]
     }; then
    # Generate FIRST and only commit a key of exactly the promised length.
    # generate_ble_api_key() shells out to python3; if that ever fails, an
    # empty value would be worse than the weak key it replaced — ble_service
    # reads an empty BLE_SERVICE_API_KEY as "auth off" (see _api_key_valid),
    # and "" is again shorter than BLE_API_KEY_LENGTH, so the rotation would
    # re-arm and thrash on every subsequent deploy. Keep the old key instead.
    # (`local` is on its own line on purpose: `local x=$(cmd)` swallows cmd's
    # exit status, so the `||` below would never see a failure.)
    local new_ble_key
    new_ble_key=$(generate_ble_api_key) || new_ble_key=""
    if [[ "${#new_ble_key}" -ne "$BLE_API_KEY_LENGTH" ]]; then
      log_warn "  Could not generate a BLE_API_KEY (python3 unavailable?) — keeping the existing value"
    else
      log_info "  Rotating weak/placeholder BLE_API_KEY"
      jq --arg key "$new_ble_key" '.BLE_API_KEY = $key' "$tmp_config" > "${tmp_config}.new" \
        && mv "${tmp_config}.new" "$tmp_config"
      updated=true
    fi
  fi

  if [[ "$updated" == "true" ]]; then
    cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
    mv "$tmp_config" "$CONFIG_FILE"
    # Preserve ownership for the service user
    local run_user="${SUDO_USER:-root}"
    if [[ "$run_user" != "root" ]]; then
      chown "$run_user:$run_user" "$CONFIG_FILE"
    fi
    log_ok "Config migrated successfully"
  else
    rm -f "$tmp_config"
    log_info "Config is up to date"
  fi
}
