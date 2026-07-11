#!/bin/bash
# release.sh - Interactive release builder for McApp
#
# Always run from the `development` branch. The script asks what you want:
#
#   Dev pre-release  → Tags development, builds tarball, publishes pre-release
#   Production       → Merges to main, tags, builds, publishes, preps next version
#
# Manages BOTH repos (MCProxy + webapp) for tags and branch switching.
# Produces a single combined tarball with backend + webapp.
# Includes automatic cleanup on failure (rollback tags, release, artifacts, branches).

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly WEBAPP_DIR="$(cd "${PROJECT_DIR}/../webapp" && pwd)"
readonly GITHUB_REPO="DK5EN/McApp"

# Colors
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly NC='\033[0m'

log_info() { echo -e "${GREEN}==>${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

#──────────────────────────────────────────────────────────────────
# CLEANUP STATE (used by trap handler)
#──────────────────────────────────────────────────────────────────

_CLEANUP_TAG=""           # git tag to delete on failure
_CLEANUP_RELEASE=""       # GitHub release to delete on failure
_CLEANUP_TARBALL=""       # tarball file to remove
_CLEANUP_CHECKSUM=""      # checksum file to remove
_CLEANUP_TMPDIR=""        # temp staging dir to remove
_CLEANUP_SWITCHED_MAIN=false  # did we switch repos to main?
_RELEASE_SUCCESS=false    # set to true at the very end

on_failure() {
  if [[ "$_RELEASE_SUCCESS" == true ]]; then
    return
  fi

  echo "" >&2
  log_error "Release failed — rolling back..."

  # Remove local artifacts
  if [[ -n "$_CLEANUP_TARBALL" && -f "${PROJECT_DIR}/${_CLEANUP_TARBALL}" ]]; then
    rm -f "${PROJECT_DIR}/${_CLEANUP_TARBALL}"
    log_warn "  Removed ${_CLEANUP_TARBALL}"
  fi
  if [[ -n "$_CLEANUP_CHECKSUM" && -f "${PROJECT_DIR}/${_CLEANUP_CHECKSUM}" ]]; then
    rm -f "${PROJECT_DIR}/${_CLEANUP_CHECKSUM}"
    log_warn "  Removed ${_CLEANUP_CHECKSUM}"
  fi

  # Remove temp staging directory
  if [[ -n "$_CLEANUP_TMPDIR" && -d "$_CLEANUP_TMPDIR" ]]; then
    rm -rf "$_CLEANUP_TMPDIR"
    log_warn "  Removed staging directory"
  fi

  # Delete GitHub release (this also removes the remote tag it created)
  if [[ -n "$_CLEANUP_RELEASE" ]]; then
    if gh release view "$_CLEANUP_RELEASE" --repo "$GITHUB_REPO" &>/dev/null; then
      gh release delete "$_CLEANUP_RELEASE" --repo "$GITHUB_REPO" --yes &>/dev/null
      log_warn "  Deleted GitHub release ${_CLEANUP_RELEASE}"
    fi
  fi

  # Delete tags from both repos
  if [[ -n "$_CLEANUP_TAG" ]]; then
    for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
      local repo_name
      repo_name=$(basename "$repo_dir")

      # Delete local tag
      if git -C "$repo_dir" tag -l "$_CLEANUP_TAG" | grep -q .; then
        git -C "$repo_dir" tag -d "$_CLEANUP_TAG" &>/dev/null
        log_warn "  Deleted local tag ${_CLEANUP_TAG} in ${repo_name}"
      fi
      # Delete remote tag
      if git -C "$repo_dir" ls-remote --tags origin "refs/tags/${_CLEANUP_TAG}" | grep -q .; then
        git -C "$repo_dir" push origin --delete "$_CLEANUP_TAG" &>/dev/null 2>&1 || true
        log_warn "  Deleted remote tag ${_CLEANUP_TAG} in ${repo_name}"
      fi
    done
  fi

  # Restore both repos to development if we switched to main
  if [[ "$_CLEANUP_SWITCHED_MAIN" == true ]]; then
    for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
      local repo_name
      repo_name=$(basename "$repo_dir")
      local current_branch
      current_branch=$(git -C "$repo_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)

      if [[ "$current_branch" == "main" ]]; then
        # Abort merge if in progress
        git -C "$repo_dir" merge --abort &>/dev/null 2>&1 || true
        git -C "$repo_dir" checkout development &>/dev/null 2>&1 || true
        log_warn "  Restored ${repo_name} to development"
      fi
    done
  fi

  log_error "Rollback complete."
}

trap on_failure EXIT

#──────────────────────────────────────────────────────────────────
# PRE-FLIGHT CLEANUP
#──────────────────────────────────────────────────────────────────

cleanup_stale_artifacts() {
  local found=false

  for f in "${PROJECT_DIR}"/mcapp-*.tar.gz "${PROJECT_DIR}"/mcapp-*.tar.gz.sha256 \
           "${PROJECT_DIR}"/mcproxy-*.tar.gz "${PROJECT_DIR}"/mcproxy-*.tar.gz.sha256 \
           "${PROJECT_DIR}"/mcadvchat-*.tar.gz "${PROJECT_DIR}"/mcadvchat-*.tar.gz.sha256; do
    if [[ -f "$f" ]]; then
      rm -f "$f"
      log_warn "Removed stale artifact: $(basename "$f")"
      found=true
    fi
  done

  if [[ "$found" == true ]]; then
    echo "" >&2
  fi
}

#──────────────────────────────────────────────────────────────────
# VALIDATION
#──────────────────────────────────────────────────────────────────

validate_tools() {
  local -a required=("git" "gh" "shasum" "tar" "sed" "npm" "jq")
  for tool in "${required[@]}"; do
    if ! command -v "$tool" &>/dev/null; then
      log_error "Required tool not found: ${tool}"
      exit 1
    fi
  done
}

validate_on_development() {
  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")
    local branch
    branch=$(git -C "$repo_dir" rev-parse --abbrev-ref HEAD)

    if [[ "$branch" != "development" ]]; then
      log_error "${repo_name} is on branch '${branch}' — must be on 'development'"
      log_error "Switch both repos to development before running this script."
      exit 1
    fi
  done
}

validate_repos_clean() {
  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")

    if [[ -n "$(git -C "$repo_dir" status --porcelain)" ]]; then
      log_error "${repo_name} has uncommitted changes:"
      git -C "$repo_dir" status --short
      exit 1
    fi
  done
}

validate_main_mergeable() {
  # Fetch latest remote state
  git -C "$PROJECT_DIR" fetch origin main --quiet
  git -C "$WEBAPP_DIR" fetch origin main --quiet

  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")

    # Check if main has commits that are NOT in development (diverged state)
    local ahead
    ahead=$(git -C "$repo_dir" rev-list --count origin/main..development 2>/dev/null || echo "0")
    local behind
    behind=$(git -C "$repo_dir" rev-list --count development..origin/main 2>/dev/null || echo "0")

    if (( behind > 0 )); then
      log_error "${repo_name}: main has ${behind} commit(s) not in development (diverged)"
      log_error "This is unexpected — main should only receive merges from development."
      log_error "Resolve this manually before releasing."
      exit 1
    fi
  done
}

#──────────────────────────────────────────────────────────────────
# INTERACTIVE PROMPT
#──────────────────────────────────────────────────────────────────

prompt_release_type() {
  echo "" >&2
  echo -e "  ${BOLD}What type of release?${NC}" >&2
  echo "" >&2
  echo "    1) Dev pre-release  (tag development, publish pre-release)" >&2
  echo "    2) Production       (merge to main, tag, publish stable release)" >&2
  echo "" >&2
  read -rp "  Choose [1/2]: " choice

  case "$choice" in
    1) echo "dev" ;;
    2) echo "production" ;;
    *)
      log_error "Invalid choice: ${choice}"
      exit 1
      ;;
  esac
}

#──────────────────────────────────────────────────────────────────
# VERSION HELPERS
#──────────────────────────────────────────────────────────────────

# Read current version from pyproject.toml (e.g., "1.4.1")
read_pyproject_version() {
  grep -oE '^version = "([^"]+)"' "${PROJECT_DIR}/pyproject.toml" \
    | head -1 | sed 's/version = "//;s/"//'
}

# Dev: find the next -dev.N tag for the current version
get_next_dev_tag() {
  local current
  current=$(read_pyproject_version)
  local prefix="v${current}-dev"

  # Find existing dev tags for this version
  local highest=0
  while IFS= read -r tag; do
    [[ -z "$tag" ]] && continue
    local num="${tag##*.}"
    if [[ "$num" =~ ^[0-9]+$ ]] && (( num > highest )); then
      highest=$num
    fi
  done < <(git -C "$PROJECT_DIR" tag -l "${prefix}.*")

  local next=$((highest + 1))
  echo "${prefix}.${next}"
}

# Find the previous production tag (e.g., v1.4.0)
find_previous_prod_tag() {
  git -C "$PROJECT_DIR" tag -l 'v*' --sort=-v:refname \
    | grep -v '\-dev\.' | head -1
}

# Bump patch version: 1.4.1 → 1.4.2
bump_patch_version() {
  local version="$1"
  local major minor patch
  IFS='.' read -r major minor patch <<< "$version"
  patch=$((patch + 1))
  echo "${major}.${minor}.${patch}"
}

#──────────────────────────────────────────────────────────────────
# RELEASE NOTES (production only)
#──────────────────────────────────────────────────────────────────

generate_release_notes_prompt() {
  local version="$1"
  local prev_tag="$2"

  local backend_log
  backend_log=$(git -C "$PROJECT_DIR" log "${prev_tag}..HEAD" --oneline 2>/dev/null || echo "(no commits)")
  local frontend_log
  frontend_log=$(git -C "$WEBAPP_DIR" log "${prev_tag}..HEAD" --oneline 2>/dev/null || echo "(no commits)")

  echo ""
  echo -e "  ${CYAN}────────────────────────────────────────${NC}"
  echo -e "  ${BOLD}Copy this prompt and run it with Claude:${NC}"
  echo ""
  echo "  Summarize the changes since ${prev_tag} for a GitHub release of v${version}."
  echo "  Backend commits (MCProxy):"
  echo "$backend_log" | sed 's/^/    /'
  echo "  Frontend commits (webapp):"
  echo "$frontend_log" | sed 's/^/    /'
  echo "  Write the summary to doc/release-history.md"
  echo -e "  ${CYAN}────────────────────────────────────────${NC}"
  echo ""
}

wait_for_release_notes() {
  echo -e "  Press ${BOLD}Enter${NC} when doc/release-history.md is ready..."
  read -r

  if [[ ! -f "${PROJECT_DIR}/doc/release-history.md" ]]; then
    log_error "doc/release-history.md not found"
    exit 1
  fi

  log_info "Found doc/release-history.md"
}

commit_release_notes() {
  # Commit release-history.md on development if it was changed
  if git -C "$PROJECT_DIR" diff --name-only | grep -q 'doc/release-history.md' || \
     git -C "$PROJECT_DIR" diff --cached --name-only | grep -q 'doc/release-history.md' || \
     git -C "$PROJECT_DIR" status --porcelain | grep -q 'doc/release-history.md'; then
    git -C "$PROJECT_DIR" add doc/release-history.md
    git -C "$PROJECT_DIR" commit -m "[docs] Add release notes for v${1}"
    log_info "Committed release notes on development"
  else
    log_info "release-history.md unchanged (already committed)"
  fi
}

#──────────────────────────────────────────────────────────────────
# BRANCH MANAGEMENT (production only)
#──────────────────────────────────────────────────────────────────

merge_to_main() {
  log_info "Merging development → main in both repos..."

  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")

    git -C "$repo_dir" checkout main
    git -C "$repo_dir" merge development --no-ff -m "Merge development for v${1}"

    log_info "  ${repo_name}: merged development → main"
  done

  _CLEANUP_SWITCHED_MAIN=true
}

checkout_development() {
  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")
    git -C "$repo_dir" checkout development
  done
  log_info "Both repos back on development"
}

merge_main_back() {
  local version="$1"
  log_info "Merging main back into development in both repos..."

  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")
    git -C "$repo_dir" merge main --no-ff -m "[chore] Merge main back into development after ${version}"
    log_info "  ${repo_name}: merged main → development"
  done
}

#──────────────────────────────────────────────────────────────────
# TAGGING (both repos)
#──────────────────────────────────────────────────────────────────

tag_both_repos() {
  local version="$1"
  local annotated="${2:-false}"

  log_info "Tagging ${version} in both repos..."

  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")

    if [[ "$annotated" == true ]]; then
      git -C "$repo_dir" tag -a "$version" -m "Release ${version}"
    else
      git -C "$repo_dir" tag "$version"
    fi

    log_info "  ${repo_name}: tagged ${version}"
  done

  _CLEANUP_TAG="$version"
}

push_main_and_tags() {
  local version="$1"

  log_info "Pushing main + tags in both repos..."

  for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
    local repo_name
    repo_name=$(basename "$repo_dir")

    git -C "$repo_dir" push origin main
    git -C "$repo_dir" push origin "$version"

    log_info "  ${repo_name}: pushed main + ${version}"
  done
}

#──────────────────────────────────────────────────────────────────
# POST-RELEASE PREP (production only)
#──────────────────────────────────────────────────────────────────

post_release_prep() {
  local current_version="$1"
  local next_version
  next_version=$(bump_patch_version "$current_version")

  log_info "Preparing next dev cycle (${next_version})..."

  # Update pyproject.toml files
  sed -i '' "s/^version = \".*\"/version = \"${next_version}\"/" \
    "${PROJECT_DIR}/pyproject.toml"
  sed -i '' "s/^version = \".*\"/version = \"${next_version}\"/" \
    "${PROJECT_DIR}/ble_service/pyproject.toml"

  git -C "$PROJECT_DIR" add pyproject.toml ble_service/pyproject.toml
  git -C "$PROJECT_DIR" commit -m "[chore] Prep v${next_version} for next dev cycle"
  git -C "$PROJECT_DIR" push origin development

  log_info "  pyproject.toml bumped to ${next_version}, pushed development"
}

#──────────────────────────────────────────────────────────────────
# WEBAPP BUILD
#──────────────────────────────────────────────────────────────────

build_webapp() {
  local version="$1"

  log_info "Building webapp..."

  if [[ ! -d "$WEBAPP_DIR" ]]; then
    log_error "Webapp directory not found: ${WEBAPP_DIR}"
    exit 1
  fi

  if [[ ! -f "${WEBAPP_DIR}/package.json" ]]; then
    log_error "No package.json found in ${WEBAPP_DIR}"
    exit 1
  fi

  # Install dependencies if node_modules is missing
  if [[ ! -d "${WEBAPP_DIR}/node_modules" ]]; then
    log_info "  Installing npm dependencies..."
    (cd "$WEBAPP_DIR" && npm install)
  fi

  # Build (webapp standardized on build:strict — type-checks then vite build)
  (cd "$WEBAPP_DIR" && npm run build:strict)

  # Validate output
  if [[ ! -d "${WEBAPP_DIR}/dist" ]] || [[ ! -f "${WEBAPP_DIR}/dist/index.html" ]]; then
    log_error "Webapp build failed — no dist/index.html found"
    exit 1
  fi

  # Write version.txt into dist for bootstrap version detection
  echo "$version" > "${WEBAPP_DIR}/dist/version.html"

  log_info "  Webapp built successfully (version.html: ${version})"
}

#──────────────────────────────────────────────────────────────────
# TARBALL BUILD
#──────────────────────────────────────────────────────────────────

build_tarball() {
  local version="$1"
  local tarball_name="mcapp-${version}.tar.gz"
  local prefix="mcapp-${version}"
  local tmp_dir
  tmp_dir=$(mktemp -d)
  local staging="${tmp_dir}/${prefix}"

  # Register for cleanup on failure
  _CLEANUP_TMPDIR="$tmp_dir"
  _CLEANUP_TARBALL="$tarball_name"

  log_info "Building release tarball..."

  mkdir -p "$staging"

  # Copy project files
  cp "${PROJECT_DIR}/pyproject.toml" "$staging/"
  cp "${PROJECT_DIR}/uv.lock" "$staging/" 2>/dev/null || log_warn "  No uv.lock found (will be generated by uv sync)"
  cp "${PROJECT_DIR}/config.sample.json" "$staging/" 2>/dev/null || true

  # src/mcapp/ package
  mkdir -p "${staging}/src/mcapp/commands"
  find "${PROJECT_DIR}/src/mcapp" -name '*.py' -not -path '*/__pycache__/*' | while read -r f; do
    local rel="${f#${PROJECT_DIR}/}"
    mkdir -p "${staging}/$(dirname "$rel")"
    cp "$f" "${staging}/${rel}"
  done

  # ble_service/
  mkdir -p "${staging}/ble_service/src"
  cp "${PROJECT_DIR}/ble_service/pyproject.toml" "${staging}/ble_service/"
  cp "${PROJECT_DIR}/ble_service/README.md" "${staging}/ble_service/"
  find "${PROJECT_DIR}/ble_service/src" -name '*.py' -not -path '*/__pycache__/*' | while read -r f; do
    local rel="${f#${PROJECT_DIR}/}"
    mkdir -p "${staging}/$(dirname "$rel")"
    cp "$f" "${staging}/${rel}"
  done

  # bootstrap/ directory
  cp -r "${PROJECT_DIR}/bootstrap" "${staging}/bootstrap"
  find "${staging}/bootstrap" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

  # scripts/ directory (update-runner)
  mkdir -p "${staging}/scripts"
  cp "${PROJECT_DIR}/scripts/update-runner.py" "${staging}/scripts/"

  # webapp/ — copy built SPA from ../webapp/dist
  if [[ -d "${WEBAPP_DIR}/dist" ]]; then
    # Use tar to copy, excluding macOS metadata
    mkdir -p "${staging}/webapp"
    tar -cf - -C "${WEBAPP_DIR}/dist" --exclude='.DS_Store' . | tar -xf - -C "${staging}/webapp"
    log_info "  Included webapp ($(find "${staging}/webapp" -type f | wc -l | tr -d ' ') files)"
  else
    log_warn "  No webapp dist found — tarball will not include webapp"
  fi

  # Build tarball
  tar -czf "${PROJECT_DIR}/${tarball_name}" -C "$tmp_dir" "$prefix"

  # Cleanup staging
  rm -rf "$tmp_dir"
  _CLEANUP_TMPDIR=""

  log_info "  Built ${tarball_name}"
  echo "$tarball_name"
}

generate_checksum() {
  local tarball="$1"
  local checksum_file="${tarball}.sha256"

  # Register for cleanup on failure
  _CLEANUP_CHECKSUM="$checksum_file"

  log_info "Generating SHA256 checksum..."

  (cd "${PROJECT_DIR}" && shasum -a 256 "$tarball" > "$checksum_file")

  log_info "  $(cat "${PROJECT_DIR}/${checksum_file}")"
  echo "$checksum_file"
}

#──────────────────────────────────────────────────────────────────
# GITHUB RELEASE
#──────────────────────────────────────────────────────────────────

upload_production() {
  local version="$1"
  local tarball="$2"
  local checksum="$3"

  log_info "Creating GitHub release ${version}..."

  local -a assets=("${PROJECT_DIR}/${tarball}" "${PROJECT_DIR}/${checksum}")

  gh release create "$version" \
    --repo "$GITHUB_REPO" \
    --title "McApp ${version}" \
    --notes-file "${PROJECT_DIR}/doc/release-history.md" \
    "${assets[@]}"

  _CLEANUP_RELEASE="$version"

  log_info "  Release published: ${version}"
}

upload_dev() {
  local version="$1"
  local tarball="$2"
  local checksum="$3"

  log_info "Creating GitHub pre-release ${version}..."

  local -a assets=("${PROJECT_DIR}/${tarball}" "${PROJECT_DIR}/${checksum}")

  # gh release create also pushes the tag to the remote
  gh release create "$version" \
    --repo "$GITHUB_REPO" \
    --title "McApp ${version} (dev)" \
    --notes "McApp development pre-release ${version}" \
    --prerelease \
    "${assets[@]}"

  _CLEANUP_RELEASE="$version"

  log_info "  Pre-release published: ${version}"
}

#──────────────────────────────────────────────────────────────────
# FINAL CLEANUP (success path)
#──────────────────────────────────────────────────────────────────

cleanup_artifacts() {
  local tarball="$1"
  local checksum="$2"

  rm -f "${PROJECT_DIR}/${tarball}" "${PROJECT_DIR}/${checksum}"

  # Clear trap state so on_failure doesn't try to remove them again
  _CLEANUP_TARBALL=""
  _CLEANUP_CHECKSUM=""
  _CLEANUP_TAG=""
  _CLEANUP_RELEASE=""

  log_info "  Cleaned up local artifacts"
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────

main() {
  echo ""
  echo "  McApp Release Builder"
  echo "  ====================="
  echo ""

  validate_tools
  validate_on_development
  validate_repos_clean

  local mode
  if [[ "${1:-}" == "1" ]]; then
    mode="dev"
  elif [[ "${1:-}" == "2" ]]; then
    mode="production"
  else
    mode=$(prompt_release_type)
  fi

  local current
  current=$(read_pyproject_version)

  echo ""
  log_info "Mode: ${mode}"
  log_info "Version in pyproject.toml: ${current}"
  echo ""

  # Remove stale artifacts from previous (failed) runs
  cleanup_stale_artifacts

  if [[ "$mode" == "production" ]]; then
    #── Production release ────────────────────────────────────────
    local version="v${current}"
    local prev_tag
    prev_tag=$(find_previous_prod_tag)

    log_info "Release: ${prev_tag:-'(none)'} → ${version}"
    echo ""

    # Verify tag doesn't already exist
    for repo_dir in "$PROJECT_DIR" "$WEBAPP_DIR"; do
      if git -C "$repo_dir" tag -l "$version" | grep -q .; then
        log_error "Tag ${version} already exists in $(basename "$repo_dir")"
        exit 1
      fi
    done

    # Validate main is mergeable (no diverged state)
    validate_main_mergeable

    # Step 1: Release notes
    if [[ -n "$prev_tag" ]]; then
      generate_release_notes_prompt "$current" "$prev_tag"
    else
      log_warn "No previous production tag found — skipping release notes prompt"
    fi
    wait_for_release_notes

    # Step 2: Commit release notes on development (if changed)
    commit_release_notes "$current"

    # Step 3: Merge development → main in both repos
    merge_to_main "$current"

    # Step 4: Build webapp
    build_webapp "$version"

    # Step 5: Build tarball
    local tarball
    tarball=$(build_tarball "$version")

    # Step 6: Tag both repos (annotated)
    tag_both_repos "$version" true

    # Step 7: Push main + tags in both repos
    push_main_and_tags "$version"

    # Step 8: Generate checksum
    local checksum
    checksum=$(generate_checksum "$tarball")

    # Step 9: Upload GitHub release
    upload_production "$version" "$tarball" "$checksum"

    # Step 10: Cleanup artifacts
    cleanup_artifacts "$tarball" "$checksum"

    # Step 11: Back to development, merge main in, prep next version
    checkout_development
    _CLEANUP_SWITCHED_MAIN=false
    merge_main_back "$version"
    post_release_prep "$current"

    _RELEASE_SUCCESS=true

    echo ""
    log_info "Release ${version} published!"
    log_info "  https://github.com/${GITHUB_REPO}/releases/tag/${version}"
    log_info "  Next dev version: $(read_pyproject_version)"
    echo ""

  else
    #── Dev pre-release ───────────────────────────────────────────
    local version
    version=$(get_next_dev_tag)

    log_info "Dev tag: ${version}"
    echo ""

    # Step 1: Build webapp
    build_webapp "$version"

    # Step 2: Tag both repos (lightweight)
    tag_both_repos "$version" false

    # Step 3: Build tarball
    local tarball
    tarball=$(build_tarball "$version")

    # Step 4: Generate checksum
    local checksum
    checksum=$(generate_checksum "$tarball")

    # Step 5: Upload as pre-release (gh release create also pushes tags)
    upload_dev "$version" "$tarball" "$checksum"

    # Step 6: Push webapp tag (gh only pushes McApp tag)
    git -C "$WEBAPP_DIR" push origin "$version"

    # Step 7: Cleanup
    cleanup_artifacts "$tarball" "$checksum"

    _RELEASE_SUCCESS=true

    echo ""
    log_info "Dev release ${version} published!"
    echo ""
  fi
}

main "$@"
