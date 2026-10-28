#!/usr/bin/env bash
# Run this ON the VM/instance (after provision_vm.sh or
# aws/provision_ec2.sh + copying the repo there + filling in
# backend/.env and docker/.env with real secrets -- see that script's
# own printed instructions). Provider-agnostic on purpose -- nothing
# below is GCP- or AWS-specific, it's the same Ubuntu box either way.
# NOT idempotent-proof against every failure mode; read the output as
# it runs.
#
# Usage (from the repo root, on the instance):
#   bash deploy/common/vm_setup.sh <static-ip>
#
# <static-ip> is the address the provisioning script reserved -- this
# turns it into a real HTTPS hostname via nip.io (a free DNS service
# that resolves "1-2-3-4.nip.io" to 1.2.3.4) so Caddy can issue a real
# Let's Encrypt certificate with no domain purchase needed.

set -euo pipefail

STATIC_IP="${1:?Usage: vm_setup.sh <static-ip>}"
HOSTNAME="${STATIC_IP//./-}.nip.io"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CURRENT_USER="$(whoami)"

echo "==> Repo root: $REPO_ROOT"
echo "==> Hostname:  $HOSTNAME"

if [ ! -f "$REPO_ROOT/backend/.env" ] || [ ! -f "$REPO_ROOT/docker/.env" ]; then
  echo "backend/.env and docker/.env must exist with real secrets before running this -- see provision_vm.sh's printed instructions." >&2
  exit 1
fi

# Security-reviewed fix: backend/.env.example ships DJANGO_ENVIRONMENT=
# development and DJANGO_DEBUG=1 as its template defaults -- neither
# looks like a "secret" so an operator filling in credential-looking
# fields could plausibly never touch them. settings.py's own fail-safes
# (weak SECRET_KEY / DEBUG=True / missing ALLOWED_HOSTS all raising
# RuntimeError) only fire once DJANGO_ENVIRONMENT is actually set to
# "production" -- checking the file merely *exists* (above) doesn't
# catch that. Enforced here instead of only documented, since this is
# about to be reachable at a public https://<ip>.nip.io URL.
if ! grep -qE '^DJANGO_ENVIRONMENT=production\s*$' "$REPO_ROOT/backend/.env"; then
  echo "backend/.env must set DJANGO_ENVIRONMENT=production before deploying publicly (it still says development/test/staging, or is missing) -- fix that first." >&2
  exit 1
fi
if ! grep -qE '^DJANGO_DEBUG=0\s*$' "$REPO_ROOT/backend/.env"; then
  echo "backend/.env must set DJANGO_DEBUG=0 before deploying publicly -- DEBUG=1 on a public URL leaks source, local variables, and settings in every 500 response." >&2
  exit 1
fi

echo "==> Installing system packages (Python, MySQL client build deps, Docker, Caddy)"
sudo apt-get update
sudo apt-get install -y \
  python3.12 python3.12-venv python3-pip \
  default-libmysqlclient-dev build-essential pkg-config \
  docker.io docker-compose-plugin \
  debian-keyring debian-archive-keyring apt-transport-https curl

sudo usermod -aG docker "$CURRENT_USER"

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update
  sudo apt-get install -y caddy
fi

echo "==> Starting MySQL + Redis (reuses docker/docker-compose.yml as-is)"
(cd "$REPO_ROOT/docker" && sudo docker compose up -d)

echo "==> Creating the backend virtualenv"
if [ ! -d "$REPO_ROOT/backend/.venv" ]; then
  python3.12 -m venv "$REPO_ROOT/backend/.venv"
fi
"$REPO_ROOT/backend/.venv/bin/pip" install --upgrade pip
"$REPO_ROOT/backend/.venv/bin/pip" install -r "$REPO_ROOT/backend/requirements.txt"

echo "==> Applying migrations"
"$REPO_ROOT/backend/.venv/bin/python" "$REPO_ROOT/backend/manage.py" check
"$REPO_ROOT/backend/.venv/bin/python" "$REPO_ROOT/backend/manage.py" migrate --noinput

echo "==> Installing systemd units"
for unit in "$REPO_ROOT"/deploy/common/systemd/*.service; do
  name="$(basename "$unit")"
  sed -e "s#__REPO_ROOT__#$REPO_ROOT#g" -e "s#__USER__#$CURRENT_USER#g" "$unit" \
    | sudo tee "/etc/systemd/system/$name" >/dev/null
done
sudo systemctl daemon-reload
for svc in algo-django algo-celery-worker-default algo-celery-worker-priority algo-celery-beat algo-run-live-feed; do
  sudo systemctl enable --now "$svc"
done

echo "==> Configuring Caddy (automatic HTTPS for $HOSTNAME)"
sed "s#__HOSTNAME__#$HOSTNAME#g" "$REPO_ROOT/deploy/common/Caddyfile.template" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl restart caddy

cat <<EOF

==================================================================
Done. Check status with:
  sudo systemctl status algo-django algo-celery-worker-default \\
    algo-celery-worker-priority algo-celery-beat algo-run-live-feed caddy

Once DNS/cert propagate (usually under a minute), the backend should
be reachable at:
  https://$HOSTNAME/api/monitoring/health/

Remember to also add this hostname to backend/.env's
ALLOWED_HOSTS / CORS_ALLOWED_ORIGINS (see docs/desktop_and_android_apps.md)
and restart algo-django if you edit it:
  sudo systemctl restart algo-django
==================================================================
EOF
