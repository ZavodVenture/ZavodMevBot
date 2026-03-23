#!/usr/bin/env bash

set -euo pipefail

API_URL="https://api.github.com/repos/ZavodVenture/ZavodMevBot/releases/latest"
TARGET_DIR="$(pwd)"
TEMP_DIR=""

cleanup() {
    if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
        rm -rf "${TEMP_DIR}"
    fi
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

install_package_if_needed() {
    local command_name="$1"
    local package_name="$2"

    if command -v "${command_name}" >/dev/null 2>&1; then
        return 0
    fi

    if ! command -v apt >/dev/null 2>&1; then
        fail "required command not found: ${command_name}. Install package: ${package_name}"
    fi

    echo "Installing missing package: ${package_name}"
    sudo apt update -y || fail "failed to update apt package index"
    sudo apt install -y "${package_name}" || fail "failed to install package: ${package_name}"

    command -v "${command_name}" >/dev/null 2>&1 || fail "command is still unavailable after installation: ${command_name}"
}

extract_json_string() {
    local key="$1"
    local file_path="$2"

    sed -n "s/.*\"${key}\":[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "${file_path}" | head -n1
}

trap cleanup EXIT

install_package_if_needed "curl" "curl"
install_package_if_needed "wget" "wget"
install_package_if_needed "unzip" "unzip"
install_package_if_needed "rsync" "rsync"

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/zavod-upgrade.XXXXXX")"
API_RESPONSE_FILE="${TEMP_DIR}/latest-release.json"
ARCHIVE_FILE="${TEMP_DIR}/release.zip"
EXTRACT_DIR="${TEMP_DIR}/extracted"

echo "Fetching the latest release information from the GitHub API..."
curl -fsSL "${API_URL}" -o "${API_RESPONSE_FILE}" || fail "failed to fetch release data"

LATEST_TAG="$(extract_json_string "tag_name" "${API_RESPONSE_FILE}")"
DOWNLOAD_URL="$(extract_json_string "browser_download_url" "${API_RESPONSE_FILE}")"

[[ -n "${LATEST_TAG}" ]] || fail "failed to determine tag_name of the latest release"
[[ -n "${DOWNLOAD_URL}" ]] || fail "failed to determine browser_download_url of the first asset"

case "${DOWNLOAD_URL}" in
    *.zip) ;;
    *)
        fail "the first asset of the latest release is not a zip archive: ${DOWNLOAD_URL}"
        ;;
esac

echo "Latest release found: ${LATEST_TAG}"
echo "Downloading archive: ${DOWNLOAD_URL}"
wget -q -O "${ARCHIVE_FILE}" "${DOWNLOAD_URL}" || fail "failed to download release archive"

mkdir -p "${EXTRACT_DIR}"
echo "Extracting archive..."
unzip -q "${ARCHIVE_FILE}" -d "${EXTRACT_DIR}" || fail "failed to extract archive"

if [[ -z "$(find "${EXTRACT_DIR}" -mindepth 1 -maxdepth 1 | head -n1)" ]]; then
    fail "release archive is empty"
fi

echo "Updating files in ${TARGET_DIR}"

# Copy files over the current version without deleting user-created files.
rsync -a \
    --exclude '.git/' \
    --exclude '.DS_Store' \
    "${EXTRACT_DIR}/" "${TARGET_DIR}/" || fail "failed to copy files from the new version"

if [[ -f "${TARGET_DIR}/upgrade.sh" ]]; then
    chmod +x "${TARGET_DIR}/upgrade.sh" || fail "failed to set executable permissions on upgrade.sh"
fi

echo "Upgrade completed: version ${LATEST_TAG} has been installed"
