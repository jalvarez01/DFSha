#!/usr/bin/env bash
# Generado por Terraform (templatefile) para la instancia del Paso 1.
# Deja la instancia lista (Docker + código clonado) pero NO levanta el
# clúster: certs/.env todavía no existen en la instancia en este punto
# (se entregan después, ver README.md de infra/01-single-instance).
set -euo pipefail

exec > /var/log/dfsha-user-data.log 2>&1

echo "== DFSha Paso 1: preparando la instancia =="

export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y ca-certificates curl gnupg git awscli

# Docker Engine + plugin "docker compose" del repositorio oficial de
# Docker (el paquete docker.io de Ubuntu no trae el plugin de compose).
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker
usermod -aG docker ubuntu || true

# Código de la aplicación: clon público, sin credenciales. Idempotente:
# si ya existe el repo, solo actualiza a la referencia pedida.
REPO_DIR=/opt/dfsha
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "${repo_url}" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --all --tags
git checkout "${git_ref}"

cat <<'EOF'
== Instancia lista. Pendiente (manual, ver infra/01-single-instance/README.md):
   1. Subir certs/.env generados por scripts/setup_cluster.py al bucket de secretos.
   2. Dentro de una sesion SSM: aws s3 cp s3://<bucket>/cluster-bundle.tar.gz /opt/dfsha/
      && tar xzf /opt/dfsha/cluster-bundle.tar.gz -C /opt/dfsha
   3. cd /opt/dfsha && docker compose up --build -d
EOF
