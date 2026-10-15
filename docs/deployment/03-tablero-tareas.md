# Tablero de tareas — Paso 1: instancia única

> Deriva de `02-plan-implementacion.md` §1. Cubre **solo el Paso 1**
> (reproducir `docker-compose.yml` completo en una sola instancia EC2).
> Los Pasos 2 y 3 se atomizan en tableros aparte cuando se llegue a
> ellos, para no mezclar decisiones de pasos distintos.
>
> Por indicación del usuario, las asunciones recomendadas en el plan se
> **conservan como decididas** para este paso (no se vuelven a poner en
> duda aquí). Donde el plan dejaba más de una opción razonable para el
> Paso 1, esta sección fija una sola para poder atomizar tareas; queda
> registrado en "Asunciones fijadas en este tablero" para que sea
> auditable.
>
> **Quién ejecuta qué**: las tareas de "escribir/editar archivos
> `.tf`" las puedo hacer yo directamente. Las tareas marcadas
> **(operador)** — crear/aplicar infraestructura real en AWS, subir
> secretos, correr comandos contra una instancia viva — las ejecuta el
> usuario, o yo con su confirmación explícita en el momento, por ser
> acciones con efecto en un sistema externo compartido (acorde a las
> reglas de ejecución ya acordadas, no por restricción nueva de este
> documento).

---

## Asunciones fijadas en este tablero (Paso 1 únicamente)

| Punto | Fijado como |
|---|---|
| Repo de la app | Público (`https://github.com/jalvarez01/DFSha`, verificado: responde 200) → `git clone` por HTTPS sin credenciales. |
| Acceso administrativo a la instancia | SSM Session Manager (sin *key pair*, sin puerto 22 abierto). |
| Entrega de `certs/`+`.env` a la instancia | Bucket S3 privado (creado por Terraform) + rol IAM de la instancia con permiso de lectura sobre ese bucket. El operador sube el `.tar` localmente con `aws s3 cp`; Terraform nunca ve su contenido. Se eligió sobre `scp` porque ya decidimos no abrir SSH. |
| AMI | Ubuntu 22.04 LTS (repositorio oficial de Docker bien documentado para `user_data`). |
| Tipo de instancia | `t3.medium` (de partida; sin medir todavía). |
| Red | VPC nueva mínima, 1 subnet pública, sin NAT (no hace falta: la instancia necesita IP pública para el cliente). |
| IP elástica | **No** en este paso — se asume que la instancia no se detiene/reinicia durante la prueba; si cambia la IP pública, se vuelve a verificar. Si esto resulta molesto en la práctica, se añade después (cambio de una línea). |
| Terraform state | Local (`terraform.tfstate` en `infra/01-single-instance/`, no versionado en git). |
| Referencia de código a desplegar | Variable `git_ref`, con default al commit actual (`3ede2b6`) para reproducibilidad; el operador puede sobreescribirla. |

Dos datos **no se pueden asumir** porque son externos a este repo y
dependen del operador — son bloqueantes para `terraform apply`, no para
escribir el código:

- `var.allowed_cidr`: IP(s) desde las que se permite conectar a los
  puertos 8000/8010/8001-8004 (típicamente la IP pública del operador en
  formato CIDR, `x.x.x.x/32`).
- Cuenta/perfil/región de AWS con la que se autentica Terraform.

---

## Fase 0 — Prerrequisitos (operador)

- [ ] **T0.1** (operador) Confirmar cuenta de AWS, región y método de
      autenticación de Terraform (perfil con nombre vs. variables de
      entorno). Sin esto no se puede hacer `terraform apply`, aunque sí
      se puede escribir y validar el código (`terraform validate`).
- [ ] **T0.2** (operador) Obtener la IP pública propia (p. ej.
      `curl https://checkip.amazonaws.com`) para `var.allowed_cidr`.
- [ ] **T0.3** Verificar localmente que `pip install cryptography` y
      `python scripts/setup_cluster.py` siguen corriendo sin tocar nada
      del repo (ya deberían, no son parte de este trabajo, pero es la
      pieza que el resto de tareas asume disponible). Hecho cuando
      exista `certs/` y `.env` locales, sin commitearlos.

## Fase 1 — Esqueleto de Terraform

- [ ] **T1.1** Crear `infra/01-single-instance/` con `main.tf`,
      `variables.tf`, `outputs.tf`, `versions.tf` (provider `aws`,
      versión fijada, backend local explícito). Hecho cuando
      `terraform init` corre sin red (solo descarga providers).
- [ ] **T1.2** Declarar variables de entrada: `aws_region`,
      `allowed_cidr` (sin default, obliga a pasarla), `instance_type`
      (default `t3.medium`), `git_ref` (default al commit fijado),
      `project_tag` (default `dfsha-test`). Hecho cuando
      `terraform plan` falla pidiendo `allowed_cidr` si no se provee.

## Fase 2 — Red

- [ ] **T2.1** VPC nueva (`10.80.0.0/24` o similar, `[elegido por mí,
      sin conflicto conocido — avisar si el operador ya usa ese
      rango]`), con `enable_dns_support`/`enable_dns_hostnames`.
- [ ] **T2.2** 1 subnet pública, 1 Internet Gateway, 1 route table con
      ruta default al IGW y asociación a la subnet.
- [ ] **T2.3** *Security group* `dfsha-single-instance`: ingress TCP
      `8000, 8010, 8001-8004` desde `var.allowed_cidr`; egress todo
      permitido (la instancia necesita salir a Docker Hub/pip/GitHub).
      Hecho cuando `terraform plan` muestra exactamente esas reglas,
      ninguna con `0.0.0.0/0` de entrada.

## Fase 3 — IAM y entrega de secretos

- [ ] **T3.1** Bucket S3 privado (`dfsha-test-secrets-<sufijo
      aleatorio>`, *block public access* activado, sin versionado —
      es efímero). Hecho cuando el plan no incluye ninguna política de
      acceso público.
- [ ] **T3.2** Rol IAM + *instance profile* para la EC2 con: política
      administrada `AmazonSSMManagedInstanceCore` (para Session
      Manager) + política inline de solo lectura (`s3:GetObject`,
      `s3:ListBucket`) acotada al bucket de T3.1. Hecho cuando la
      política no otorga ningún permiso de escritura ni sobre otros
      recursos.
- [ ] **T3.3** (operador) Documentar (no automatizar) el comando exacto
      para subir el material tras `setup_cluster.py`:
      `tar czf cluster-bundle.tar.gz certs .env && aws s3 cp
      cluster-bundle.tar.gz s3://<bucket>/cluster-bundle.tar.gz`. Este
      comando vive en el *runbook* de ejecución (siguiente documento o
      un `README` dentro de `infra/01-single-instance/`), no en `.tf`.

## Fase 4 — Cómputo

- [ ] **T4.1** `data "aws_ami"` para la última Ubuntu 22.04 LTS oficial
      (Canonical, owner `099720109477`), para no fijar un AMI ID que
      quede obsoleto.
- [ ] **T4.2** Script `user_data` (plantilla `templatefile`) que, en
      orden:
      1. Instala Docker Engine + plugin `docker compose` (repositorio
         oficial de Docker, no el paquete `docker.io` de Ubuntu, que no
         trae el plugin de `compose`).
      2. Instala `awscli` (o usa el que ya trae Ubuntu 22.04 vía `snap`
         — a confirmar cuál es más rápido en el arranque).
      3. Clona el repo público en `/opt/dfsha` al `var.git_ref`.
      4. Deja todo listo para la Fase 5, **sin** intentar levantar
         `docker compose` todavía (los secretos aún no están en la
         instancia en el momento del `user_data`).
      Hecho cuando el script es idempotente si se reintenta (usa
      `apt-get install` con `-y`, `git clone` solo si el directorio no
      existe).
- [ ] **T4.3** `aws_instance` con el `instance_profile` de T3.2, la
      `security_group` de T2.3, la subnet de T2.2, `user_data` de T4.2,
      sin `key_name` (no hace falta: acceso por SSM). Tags mínimas:
      `Name=dfsha-single-instance`, `Project=var.project_tag`.
- [ ] **T4.4** `outputs.tf`: IP pública de la instancia, ID de la
      instancia (para `aws ssm start-session --target <id>`), nombre
      del bucket de secretos. Hecho cuando `terraform output` da los
      tres valores sin necesidad de ir a la consola de AWS.

## Fase 5 — Primer `apply` y entrega de secretos (operador)

- [ ] **T5.1** (operador) `terraform -chdir=infra/01-single-instance
      init && plan && apply` con `allowed_cidr` real. Confirmar antes
      de ejecutar (recurso nuevo en una cuenta real).
- [ ] **T5.2** (operador) Ejecutar el comando de T3.3 para subir
      `cluster-bundle.tar.gz` al bucket recién creado.
- [ ] **T5.3** (operador) Abrir sesión SSM contra la instancia
      (`aws ssm start-session --target <instance-id>`), descargar y
      extraer el bundle dentro de `/opt/dfsha`
      (`aws s3 cp s3://<bucket>/cluster-bundle.tar.gz . && tar xzf
      cluster-bundle.tar.gz`), de forma que `certs/` y `.env` queden
      junto al `docker-compose.yml` clonado en T4.2.

## Fase 6 — Levantar y verificar la aplicación (operador)

- [ ] **T6.1** (operador) Dentro de la sesión SSM:
      `cd /opt/dfsha && docker compose up --build -d`. Hecho cuando
      `docker compose ps` muestra los 6 servicios `Up`/`healthy`.
- [ ] **T6.2** (operador) Desde la máquina local (no la instancia):
      `pip install -e ./cli` y `dfsha login <usuario> -u
      https://<ip-pública>:8000,https://<ip-pública>:8010 --ca-cert
      certs/ca.crt` (usando el `certs/ca.crt` generado localmente en
      T0.3, que es el mismo que se subió). Hecho cuando el login
      responde `200`.
- [ ] **T6.3** (operador) `dfsha put`/`get` de un archivo de prueba y
      `dfsha nodes` mostrando los 4 DataNodes `vivo`.
- [ ] **T6.4** (operador) Correr `python scripts/verify_hito3.py`
      apuntando al clúster recién desplegado (requiere adaptar las
      URLs que el script usa por defecto — hoy asume `localhost`;
      revisar si acepta parámetro o variable de entorno antes de
      asumir que funciona sin cambios). **`[VALIDAR]`** si el script
      necesita un argumento de host o si hay que correrlo *desde la
      propia instancia* (donde `localhost` sí es válido) en su lugar.

## Fase 7 — Cierre (operador)

- [ ] **T7.1** (operador) Confirmar que no quedó ningún recurso fuera
      de Terraform (el bucket de secretos y su contenido sí es
      intencional y persiste hasta que se decida borrarlo a mano, ya
      que no está pensado para destruirse con `terraform destroy` sin
      antes verificar que no se necesita su contenido).
- [ ] **T7.2** (operador) `terraform -chdir=infra/01-single-instance
      destroy`, con confirmación explícita previa, cuando la prueba del
      Paso 1 haya terminado y se vaya a pasar al Paso 2.

---

## Notas abiertas para el tablero del Paso 2

Estas no se resuelven aquí, pero quedan anotadas porque T6.4 y la
elección de AMI/bucket de este tablero las van a tocar de nuevo:

- Si `scripts/verify_hito3.py` resulta no aceptar un host distinto de
  `localhost`, el Paso 1 se verifica ejecutando el script **dentro** de
  la instancia (vía SSM), y el Paso 2 necesitará resolver esto de verdad
  porque ahí ya no hay un único `localhost` que sirva para todo.
- El patrón "bucket S3 + IAM de solo lectura para entregar secretos sin
  que Terraform los lea" de la Fase 3 se reutiliza directamente en el
  Paso 2, con un prefijo por nodo en vez de un bundle único.
