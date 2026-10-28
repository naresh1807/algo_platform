#!/usr/bin/env bash
# Creates the Compute Engine VM + firewall rules + a static external IP
# for the algo trading platform backend.
#
# NOT auto-run by anything -- this provisions real, billable GCP
# resources and needs your own authenticated GCP project, so it's a
# script you run yourself once you've decided to proceed (see
# docs/desktop_and_android_apps.md's "Moving the backend to Google
# Cloud" section for the cost/security tradeoffs this makes).
#
# Prereqs:
#   1. gcloud CLI installed and authenticated: gcloud auth login
#   2. A GCP project with billing enabled: gcloud config set project <id>
#      (or pass PROJECT_ID below)
#
# Usage:
#   PROJECT_ID=my-project ./provision_vm.sh
#
# What this does NOT do (deliberately, so nothing billable happens by
# surprise): it does not copy your code, fill in secrets, or start any
# service -- see vm_setup.sh for that, run separately after this.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-asia-south1}"
ZONE="${ZONE:-asia-south1-a}"
INSTANCE_NAME="${INSTANCE_NAME:-algo-platform}"
# e2-medium (2 vCPU burstable, 4GB RAM) -- MySQL + Redis + Django +
# 2 Celery workers + Celery Beat + the live feed process all run on this
# one box. e2-small (2GB RAM) was considered but is tight once
# torch/transformers is loaded for the ML tasks -- see
# backend/requirements.txt's ML dependencies.
MACHINE_TYPE="${MACHINE_TYPE:-e2-medium}"
# Security-reviewed fix: this used to omit --source-ranges entirely, which
# gcloud defaults to 0.0.0.0/0 -- SSH open to the whole internet. Matches
# deploy/aws/provision_ec2.sh's MY_IP detection now instead of just
# leaving a comment about it. Override with MY_IP=x.x.x.x/32 if it changes.
MY_IP="${MY_IP:-$(curl -s https://checkip.amazonaws.com)/32}"

echo "==> Setting active project to $PROJECT_ID"
gcloud config set project "$PROJECT_ID"
echo "==> SSH will be restricted to: $MY_IP"

echo "==> Reserving a static external IP (needed for a stable HTTPS hostname)"
gcloud compute addresses create "${INSTANCE_NAME}-ip" --region "$REGION" || true
STATIC_IP=$(gcloud compute addresses describe "${INSTANCE_NAME}-ip" --region "$REGION" --format="value(address)")
echo "    Static IP: $STATIC_IP"
echo "    HTTPS hostname will be: ${STATIC_IP//./-}.nip.io (see vm_setup.sh)"

echo "==> Creating firewall rules (80/443/22 only -- never 8000/3306/6379 to the internet)"
gcloud compute firewall-rules create algo-platform-web \
  --allow=tcp:80,tcp:443 --target-tags=algo-platform \
  --description="Caddy reverse proxy -- the only public entry point" || true
gcloud compute firewall-rules create algo-platform-ssh \
  --allow=tcp:22 --target-tags=algo-platform --source-ranges="$MY_IP" \
  --description="SSH for deployment/maintenance -- restricted to the operator's IP at creation time" || true

echo "==> Creating the VM ($MACHINE_TYPE, Ubuntu 24.04 LTS)"
gcloud compute instances create "$INSTANCE_NAME" \
  --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-balanced \
  --address="$STATIC_IP" \
  --tags=algo-platform

cat <<EOF

==================================================================
VM created. Rough ongoing cost: ~US\$25-35/month total
(e2-medium + 30GB disk + a handful of GB egress -- check the GCP
pricing calculator for your exact region before relying on this).

Next steps:
  1. Copy the repo to the VM (from the repo root):
       gcloud compute scp --recurse . ${INSTANCE_NAME}:~/algo_platform --zone=$ZONE

  2. SSH in and fill in real secrets (never committed -- see
     backend/.env.example and docker/.env.example):
       gcloud compute ssh ${INSTANCE_NAME} --zone=$ZONE
       cd ~/algo_platform
       cp backend/.env.example backend/.env      # edit with real values
       cp docker/.env.example docker/.env        # edit with real values

  3. Run the setup script on the VM:
       bash deploy/common/vm_setup.sh $STATIC_IP

  4. Point the desktop/Android apps' Server Settings at:
       https://${STATIC_IP//./-}.nip.io
==================================================================
EOF
