# Paso 1 — instancia única (runbook)

Implementa `docs/deployment/02-plan-implementacion.md` §1 y
`docs/deployment/03-tablero-tareas.md`. Este archivo cubre las fases que
ejecuta el **operador** (no Terraform): entrega de secretos, arranque de
la aplicación y verificación. Léelo junto al tablero de tareas, que
tiene el detalle de cada paso y las asunciones fijadas.

Requisitos previos:

- AWS CLI configurado (credenciales/perfil con permiso para crear VPC,
  EC2, IAM, S3) y `terraform >= 1.5`.
- `pip install cryptography` en tu máquina (para `setup_cluster.py`).

## 0. Preparar variables y secretos (local)

```bash
curl https://checkip.amazonaws.com   # tu IP pública, para allowed_cidr
cp terraform.tfvars.example terraform.tfvars
# edita terraform.tfvars con tu IP real

cd ../..   # raíz del repo
python scripts/setup_cluster.py      # genera certs/ y .env, NO se suben a git
```

## 1. Aplicar Terraform

```bash
cd infra/01-single-instance
terraform init
terraform plan
terraform apply
```

Corre `terraform output` y anota estos cuatro valores — los vas a
necesitar, ya literales, en el resto de esta guía:

| Placeholder | De dónde sale |
|---|---|
| `<instance_id>` | output `instance_id` |
| `<public_ip>` | output `public_ip` |
| `<secrets_bucket>` | output `secrets_bucket` |
| `<aws_region>` | el valor de `aws_region` que usaste en `terraform.tfvars` (por defecto `us-east-1`) |

## 2. Subir el material de `setup_cluster.py` (nunca a través de Terraform)

```bash
cd ../..   # raíz del repo
tar czf cluster-bundle.tar.gz certs .env
aws s3 cp cluster-bundle.tar.gz "s3://<secrets_bucket>/cluster-bundle.tar.gz"
rm cluster-bundle.tar.gz   # no dejar la copia sin cifrar en el disco local más de lo necesario
```

## 3. Abrir sesión administrativa y levantar el clúster

```bash
aws ssm start-session --target <instance_id> --region <aws_region>
```

Dentro de la sesión SSM (en la instancia). La sesión entra como
`ssm-user`, pero `/opt/dfsha` lo clonó `root` durante el arranque
(`user_data`) y el socket de Docker tampoco es escribible por
`ssm-user` — por eso todo este bloque corre como `root`:

```bash
sudo -s
cd /opt/dfsha
aws s3 cp "s3://<secrets_bucket>/cluster-bundle.tar.gz" .
tar xzf cluster-bundle.tar.gz
IP=<public_ip>
sed -i "s/DFSHA_DATANODE_ADVERTISE_HOST: \"[^\"]*\"/DFSHA_DATANODE_ADVERTISE_HOST: \"$IP\"/" docker-compose.yml
sed -i -E "s#DFSHA_PUBLIC_URL: \"https://localhost:([0-9]+)\"#DFSHA_PUBLIC_URL: \"https://$IP:\1\"#" docker-compose.yml
docker compose up --build -d
docker compose ps   # deben verse 6 servicios Up/healthy
exit   # sale de la shell root
exit   # cierra la sesión SSM
```

## 4. Verificar desde tu máquina

```bash
pip install -e ./cli
dfsha login <usuario> -u https://<public_ip>:8000,https://<public_ip>:8010 --ca-cert certs/ca.crt
dfsha nodes
dfsha put algun_archivo.bin && dfsha get algun_archivo.bin copia.bin
```

`python scripts/verify_hito3.py` asume `localhost` por defecto —
revisa si acepta un host distinto antes de correrlo contra la IP
pública; si no, córrelo dentro de la instancia (vía la sesión SSM del
paso 3), donde `localhost` sí es válido. Esto está anotado como
pendiente (`[VALIDAR]`, tarea T6.4) en el tablero de tareas.

## 5. Cerrar el entorno

```bash
# Solo cuando ya no necesites el bundle de secretos:
aws s3 rm "s3://<secrets_bucket>/cluster-bundle.tar.gz"

terraform destroy
```

`terraform destroy` falla a propósito si el bucket de secretos todavía
tiene objetos (no tiene `force_destroy`): es la confirmación de que no
se te olvidó nada antes de borrar.
