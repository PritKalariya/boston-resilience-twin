#!/usr/bin/env bash
# airflow-vm boot provisioning (#60). Set as the instance's `startup-script`
# metadata key, so GCE runs it as root on every boot. Every step is safe to repeat.
#
# It installs Docker, creates the `airflow` host user and clones the repo. It does
# not write .env, fetch secrets or run `docker compose up`: those are runbook steps
# until #64 moves the secrets to Secret Manager.
#
# Read its log with:
#   sudo journalctl -u google-startup-scripts.service --no-pager | grep brt-startup
# Rerun it without a reboot with:
#   sudo google_metadata_script_runner startup
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

log "done"
