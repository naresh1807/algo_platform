#!/usr/bin/env bash
# Creates the EC2 instance + security group + Elastic IP for the algo
# trading platform backend. Same design as deploy/gcp/provision_vm.sh
# (self-hosted MySQL/Redis via Docker on one box, Caddy for automatic
# HTTPS via a free nip.io hostname, systemd units in
# deploy/common/systemd/) -- only the cloud-specific provisioning layer
# differs, everything that runs ON the box is shared and identical.
#
# NOT auto-run by anything -- this provisions real, billable AWS
# resources. Run it yourself once you've decided to proceed (see
# docs/desktop_and_android_apps.md's cloud section for the cost/security
# tradeoffs this makes).
#
# Prereqs:
#   1. AWS CLI v2 installed (this session installed it if it wasn't
#      already there).
#   2. Configured credentials: run `aws configure` yourself first --
#      needs an IAM access key (AWS Console > IAM > Users > your user >
#      Security credentials > Create access key). This is the one step
#      that needs your own login and can't be done on your behalf.
#
# Usage:
#   ./provision_ec2.sh
#   (override REGION/INSTANCE_TYPE/KEY_NAME/MY_IP via env vars)
#
# Security default worth knowing: SSH (port 22) is restricted to
# whatever public IP this script detects you're running from right now
# -- not open to the whole internet. 80/443 (Caddy, the only path to
# the Django backend) are public, matching provision_vm.sh's GCP
# firewall rules. If your IP changes later (e.g. provisioning from a
# different network, or your ISP reassigns it), re-run with
# MY_IP=x.x.x.x/32 or update the security group's inbound rule in the
# AWS Console.

set -euo pipefail

REGION="${REGION:-ap-south-1}"
# t3.medium (2 vCPU burstable, 4GB RAM) -- the AWS equivalent of
# provision_vm.sh's e2-medium pick; same reasoning (MySQL + Redis +
# Django + 2 Celery workers + Celery Beat + the live feed process all
# on one box, torch/transformers make 2GB tight).
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
INSTANCE_NAME="${INSTANCE_NAME:-algo-platform}"
KEY_NAME="${KEY_NAME:-algo-platform-key}"
MY_IP="${MY_IP:-$(curl -s https://checkip.amazonaws.com)/32}"

echo "==> Region: $REGION"
echo "==> SSH will be restricted to: $MY_IP"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS CLI isn't authenticated yet. Run 'aws configure' first (needs an IAM access key from the AWS Console) then re-run this script." >&2
  exit 1
fi

# --- Key pair ---------------------------------------------------------
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Creating key pair $KEY_NAME (saved to ./${KEY_NAME}.pem -- keep it safe, it's the only way to SSH in)"
  aws ec2 create-key-pair --key-name "$KEY_NAME" --region "$REGION" \
    --query "KeyMaterial" --output text > "./${KEY_NAME}.pem"
  chmod 400 "./${KEY_NAME}.pem" 2>/dev/null || true
fi

# --- Security group -----------------------------------------------------
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=${INSTANCE_NAME}-sg" \
  --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || echo "None")
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  echo "==> Creating security group (80/443 public, 22 restricted to $MY_IP)"
  VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text)
  SG_ID=$(aws ec2 create-security-group --region "$REGION" \
    --group-name "${INSTANCE_NAME}-sg" \
    --description "Algo trading platform -- Caddy (80/443) public, SSH restricted to the operator's IP" \
    --vpc-id "$VPC_ID" --query "GroupId" --output text)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "$MY_IP" >/dev/null
  echo "    Security group: $SG_ID"
fi

# --- AMI: latest Ubuntu 24.04 LTS (Canonical's official account) --------
AMI_ID=$(aws ec2 describe-images --region "$REGION" \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)

# --- Launch instance ------------------------------------------------------
echo "==> Launching $INSTANCE_TYPE ($AMI_ID)"
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]" \
  --query "Instances[0].InstanceId" --output text)
echo "    Instance: $INSTANCE_ID -- waiting for it to be running..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

# --- Elastic IP -------------------------------------------------------
ALLOC_ID=$(aws ec2 allocate-address --region "$REGION" --domain vpc --query "AllocationId" --output text)
aws ec2 associate-address --region "$REGION" --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
STATIC_IP=$(aws ec2 describe-addresses --region "$REGION" --allocation-ids "$ALLOC_ID" --query "Addresses[0].PublicIp" --output text)

cat <<EOF

==================================================================
Instance created. Rough ongoing cost: ~US\$30-40/month total
(t3.medium + 30GB gp3 disk + a handful of GB egress -- an Elastic IP
is free while attached to a running instance, billed if you later
release/detach it while it sits idle. Check the AWS pricing calculator
for your exact region before relying on this).

Static IP: $STATIC_IP
SSH:       ssh -i ${KEY_NAME}.pem ubuntu@$STATIC_IP
           (only reachable from $MY_IP -- see this script's own header)

Next steps:
  1. Copy the repo to the instance (from the repo root):
       scp -i deploy/aws/${KEY_NAME}.pem -r . ubuntu@${STATIC_IP}:~/algo_platform

  2. SSH in and fill in real secrets (never committed -- see
     backend/.env.example and docker/.env.example):
       ssh -i deploy/aws/${KEY_NAME}.pem ubuntu@${STATIC_IP}
       cd ~/algo_platform
       cp backend/.env.example backend/.env      # edit with real values
       cp docker/.env.example docker/.env        # edit with real values

  3. Run the shared setup script (same one the GCP path uses):
       bash deploy/common/vm_setup.sh $STATIC_IP

  4. Point the desktop/Android apps' Server Settings at:
       https://${STATIC_IP//./-}.nip.io
==================================================================
EOF
