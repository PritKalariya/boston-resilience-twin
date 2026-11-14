#!/usr/bin/env bash
# airflow-vm boot provisioning (#60) and .env render (#64, ADR-034). Set as the
# instance's `startup-script` metadata key, so GCE runs it as root on every boot.
# Every step is safe to repeat.
#
# It installs Docker, creates the `airflow` host user, clones the repo and, last,
# renders the repo's `.env` from Secret Manager. It never runs `docker compose`:
# containers read `.env` only when they are created, so after a changed secret
# run `sudo docker compose up -d --wait` (runbook, "Airflow on the VM").
#
# Read its log with:
#   sudo journalctl -u google-startup-scripts.service --no-pager | grep brt-startup
# Rerun it without a reboot with:
#   sudo google_metadata_script_runner startup
#
# Test the render alone, with no root and no Docker (Deployment/terraform/README.md):
#   BRT_RENDER_ONLY=1 BRT_ENV_FILE=/tmp/t/.env BRT_ENV_OWNER="$(id -u):$(id -g)" \
#     BRT_SECRET_FETCH_CMD=./stub_fetch.sh bash startup.sh
set -euo pipefail

REPO_URL="https://github.com/PritKalariya/boston-resilience-twin.git"
BRANCH="main"
CLONE_DIR="/opt/boston-resilience-twin"
AIRFLOW_USER="airflow"
AIRFLOW_UID="50000"

log() { echo "brt-startup: $*"; }
die() { log "error: $*"; exit 1; }

# unattended-upgrades holds the apt lock for a few minutes on first boot.
apt_get() { DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 -q "$@"; }

# ---- .env render (#64, ADR-034) ----

# The render's inputs, overridable so a stub can drive it on a laptop. The fetch
# command is called as `<command> <secret name>`: it prints the value and exits 0,
# exits 4 when the secret has no version, and exits 1 on any other failure.
ENV_FILE="${BRT_ENV_FILE:-$CLONE_DIR/.env}"
ENV_OWNER="${BRT_ENV_OWNER:-$AIRFLOW_UID:$AIRFLOW_UID}"
GCP_PROJECT="${BRT_GCP_PROJECT:-boston-resilience-twin}"
FETCH_CMD="${BRT_SECRET_FETCH_CMD:-fetch_with_gcloud}"
FETCH_NO_VERSION=4

# VARIABLE=secret-name. The variables are the names docker-compose.yaml reads.
REQUIRED_SECRETS=(
  "POSTGRES_PASSWORD=airflow-postgres-password"
  "_AIRFLOW_WWW_USER_PASSWORD=airflow-admin-password"
  "AIRFLOW__API_AUTH__JWT_SECRET=airflow-jwt-secret"
  "AIRFLOW__API__SECRET_KEY=airflow-api-secret-key"
  "FERNET_KEY=airflow-fernet-key"
)
OPTIONAL_SECRETS=(
  "NOAA_CDO_TOKEN=noaa-cdo-token"
  "CENSUS_API_KEY=census-api-key"
  "NWS_USER_AGENT=nws-user-agent"
  "SLACK_ALERT_WEBHOOK=slack-alert-webhook"
)

# The default fetcher. Prints the value and nothing else. When the read fails it
# asks for the newest enabled version: an empty answer means the secret has no
# version (exit 4); a failed listing, or a version that exists, is a plain failure
# (exit 1). Needs `versions list`, which airflow-vm-sa holds on the optional slots.
#
# stdout carries the value only, so everything this function logs goes to stderr.
# When the result is a plain failure (exit 1) it logs the first line of gcloud's
# stderr (an error message, never the value, which is on stdout) so the journal
# says why. A secret with no version (exit 4) logs nothing here: the caller warns.
fetch_with_gcloud() {
  local name="$1" bin="" candidate listing errfile access_line list_line
  bin="$(command -v gcloud || true)"
  if [ -z "$bin" ]; then
    for candidate in /snap/bin/gcloud /usr/bin/gcloud /usr/lib/google-cloud-sdk/bin/gcloud; do
      if [ -x "$candidate" ]; then bin="$candidate"; break; fi
    done
  fi
  if [ -z "$bin" ]; then
    log "error: gcloud not found on this VM" >&2
    return 1
  fi
  errfile="$(mktemp)" || { log "error: cannot create a temp file for gcloud's error output" >&2; return 1; }
  if env HOME="${HOME:-/root}" CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    "$bin" secrets versions access latest --secret="$name" --project="$GCP_PROJECT" 2>"$errfile"; then
    rm -f "$errfile"
    return 0
  fi
  access_line="$(head -n 1 "$errfile")"
  if listing="$(env HOME="${HOME:-/root}" CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    "$bin" secrets versions list "$name" --project="$GCP_PROJECT" \
    --filter=state=ENABLED --limit=1 --format='value(name)' 2>"$errfile")"; then
    rm -f "$errfile"
    if [ -z "$listing" ]; then
      return "$FETCH_NO_VERSION"
    fi
    log "gcloud access of $name failed: ${access_line:-no error text}" >&2
    return 1
  fi
  list_line="$(head -n 1 "$errfile")"
  rm -f "$errfile"
  log "gcloud access of $name failed: ${access_line:-no error text}" >&2
  log "gcloud list of $name failed: ${list_line:-no error text}" >&2
  return 1
}

# Sets FETCHED to the secret's value without its trailing newlines. Returns the
# fetcher's code: 0, FETCH_NO_VERSION or another failure.
FETCHED=""
fetch_secret() {
  local -a cmd
  local rc=0
  FETCHED=""
  read -r -a cmd <<<"$FETCH_CMD"
  if FETCHED="$("${cmd[@]}" "$1")"; then rc=0; else rc=$?; FETCHED=""; fi
  return "$rc"
}

# A line of .env is VAR='value': compose cannot escape a single quote inside
# single quotes, and a line break would start a new line. Prints the reason and
# returns 1 when the value cannot be written safely. Never prints the value.
value_problem() {
  local var="$1" value="$2"
  case "$value" in
    *"'"*) echo "contains a single quote"; return 1 ;;
    *$'\n'* | *$'\r'*) echo "contains a line break"; return 1 ;;
  esac
  # The Postgres password sits inside AIRFLOW__DATABASE__SQL_ALCHEMY_CONN, a URL.
  if [ "$var" = POSTGRES_PASSWORD ] && ! [[ "$value" =~ ^[A-Za-z0-9]+$ ]]; then
    echo "is not letters and digits only (it sits in a connection URL)"
    return 1
  fi
  # One Fernet key is 44 URL-safe base64 characters; a rotation holds `new,old`.
  if [ "$var" = FERNET_KEY ] && ! [[ "$value" =~ ^[A-Za-z0-9_=-]{44}(,[A-Za-z0-9_=-]{44})*$ ]]; then
    echo "is not one Fernet key or a comma-separated list of them"
    return 1
  fi
  return 0
}

# Renders ENV_FILE whole: every secret is fetched and checked first, so one
# failure leaves the existing file untouched. Logs secret names, never values.
render_env() {
  local dir pair var name rc failed=0 old_umask tmp line count=0 empty=0
  local -a lines
  local problem
  dir="$(dirname "$ENV_FILE")"
  if [ ! -d "$dir" ]; then
    log "error: $dir does not exist; .env not written"
    return 1
  fi

  lines=("AIRFLOW_UID=$AIRFLOW_UID")

  for pair in "${REQUIRED_SECRETS[@]}"; do
    var="${pair%%=*}"
    name="${pair#*=}"
    if fetch_secret "$name"; then rc=0; else rc=$?; fi
    if [ "$rc" -ne 0 ]; then
      log "error: required secret $name could not be read (fetch exit $rc)"
      failed=1
      continue
    fi
    if [ -z "$FETCHED" ]; then
      log "error: required secret $name is empty"
      failed=1
      continue
    fi
    if problem="$(value_problem "$var" "$FETCHED")"; then
      lines+=("$var='$FETCHED'")
    else
      log "error: required secret $name $problem"
      failed=1
    fi
  done

  for pair in "${OPTIONAL_SECRETS[@]}"; do
    var="${pair%%=*}"
    name="${pair#*=}"
    if fetch_secret "$name"; then rc=0; else rc=$?; fi
    if [ "$rc" -eq "$FETCH_NO_VERSION" ]; then
      log "warning: optional secret $name has no version; $var written empty"
      lines+=("$var=''")
      empty=$((empty + 1))
    elif [ "$rc" -ne 0 ]; then
      log "error: optional secret $name could not be read (fetch exit $rc)"
      failed=1
    elif [ -z "$FETCHED" ]; then
      log "warning: optional secret $name is empty; $var written empty"
      lines+=("$var=''")
      empty=$((empty + 1))
    elif problem="$(value_problem "$var" "$FETCHED")"; then
      lines+=("$var='$FETCHED'")
    else
      log "error: optional secret $name $problem"
      failed=1
    fi
  done
  FETCHED=""

  if [ "$failed" -ne 0 ]; then
    log "error: $ENV_FILE not changed"
    return 1
  fi

  # Same directory, so the final mv is a rename on one filesystem.
  old_umask="$(umask)"
  umask 077
  tmp="$(mktemp "$dir/.env.XXXXXX")" || { umask "$old_umask"; log "error: cannot create a temp file in $dir"; return 1; }
  umask "$old_umask"
  for line in "${lines[@]}"; do
    printf '%s\n' "$line" >>"$tmp"
    count=$((count + 1))
  done
  if ! chown "$ENV_OWNER" "$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" "$ENV_FILE"; then
    rm -f "$tmp"
    log "error: could not install the new $ENV_FILE; it is not changed"
    return 1
  fi
  log "rendered $ENV_FILE: $count keys, $empty optional empty, owner $ENV_OWNER, mode 600"
}

# Render alone: no root, no Docker, no clone. For the stub test.
if [ "${BRT_RENDER_ONLY:-0}" = 1 ]; then
  render_env
  exit $?
fi

[ "$(id -u)" -eq 0 ] || die "must run as root"

# A stop during an earlier first boot can leave packages unpacked but not configured,
# and apt refuses to install anything until they are. dpkg takes no lock timeout, so
# retry while unattended-upgrades holds its lock. Runs once, and only before an install.
dpkg_ready=0
prepare_apt() {
  [ "$dpkg_ready" -eq 0 ] || return 0
  local attempt
  for attempt in $(seq 1 60); do
    if dpkg --configure -a; then
      dpkg_ready=1
      return 0
    fi
    log "dpkg --configure -a failed (attempt $attempt of 60), retrying in 10 s"
    sleep 10
  done
  die "dpkg --configure -a kept failing"
}

# ---- Docker Engine, compose plugin, git ----

have_docker() { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }

install_docker() {
  # shellcheck source=/dev/null
  . /etc/os-release
  local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  case "${ID:-}" in
    ubuntu | debian) ;;
    *) die "unsupported distro '${ID:-unknown}': Docker's apt repo is set up for ubuntu and debian only" ;;
  esac
  [ -n "$codename" ] || die "no release codename in /etc/os-release"

  log "installing Docker Engine and the compose plugin from download.docker.com ($ID $codename)"
  prepare_apt
  apt_get update
  apt_get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $codename stable" \
    > /etc/apt/sources.list.d/docker.list
  apt_get update
  apt_get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

if have_docker; then
  log "docker and the compose plugin already installed"
else
  install_docker
  have_docker || die "docker or the compose plugin is still missing after the install"
  log "docker installed"
fi

if ! command -v git >/dev/null 2>&1; then
  log "installing git"
  prepare_apt
  apt_get update
  apt_get install -y git
fi

systemctl enable --now docker
log "docker enabled and running"

# ---- Host user that owns the clone (#60) ----

owner_of_uid="$(getent passwd "$AIRFLOW_UID" | cut -d: -f1 || true)"
if [ -n "$owner_of_uid" ] && [ "$owner_of_uid" != "$AIRFLOW_USER" ]; then
  die "UID $AIRFLOW_UID belongs to '$owner_of_uid', not '$AIRFLOW_USER'"
fi
if id -u "$AIRFLOW_USER" >/dev/null 2>&1; then
  [ "$(id -u "$AIRFLOW_USER")" = "$AIRFLOW_UID" ] \
    || die "user '$AIRFLOW_USER' exists with UID $(id -u "$AIRFLOW_USER"), expected $AIRFLOW_UID"
  log "user $AIRFLOW_USER (UID $AIRFLOW_UID) already exists"
else
  useradd --system --uid "$AIRFLOW_UID" --user-group --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin "$AIRFLOW_USER"
  log "created user $AIRFLOW_USER (UID $AIRFLOW_UID, no login shell)"
fi

# ---- Clone ----

# Cloned beside the final path and moved into place, so a clone killed part way (a VM
# stopped during first boot) never leaves a .git at CLONE_DIR, which the next boot
# would take for a finished checkout.
PARTIAL_DIR="${CLONE_DIR}.partial"
as_airflow() { runuser -u "$AIRFLOW_USER" -- env HOME=/nonexistent "$@"; }

if [ -e "$CLONE_DIR/.git" ]; then
  log "checkout present at $CLONE_DIR, leaving it untouched"
elif [ -e "$CLONE_DIR" ]; then
  die "$CLONE_DIR exists but is not a git repository; not touching it"
else
  if [ -e "$PARTIAL_DIR" ]; then
    log "removing $PARTIAL_DIR left by an interrupted run"
    rm -rf "$PARTIAL_DIR"
  fi
  log "cloning $REPO_URL ($BRANCH) into $PARTIAL_DIR as $AIRFLOW_USER"
  mkdir "$PARTIAL_DIR"
  chown "$AIRFLOW_USER:" "$PARTIAL_DIR"
  if ! as_airflow git clone --quiet --branch "$BRANCH" "$REPO_URL" "$PARTIAL_DIR"; then
    rm -rf "$PARTIAL_DIR"
    die "git clone failed"
  fi
  mv "$PARTIAL_DIR" "$CLONE_DIR"
  log "cloned $(as_airflow git -C "$CLONE_DIR" rev-parse --short HEAD) from $BRANCH into $CLONE_DIR"
fi

# ---- .env (#64, ADR-034) ----

# Last, so the checkout exists. A failure exits non-zero and leaves the old .env.
render_env || die ".env was not rendered; the existing one is untouched"

log "done"
