# dfsha-cli

Cliente CLI de DFSha (Hitos 1, 2 y 3). Implementa la parte de **Mariana**:
gestión del filesystem y transferencia desde el lado del cliente, más
autenticación/sesión local. Desde el Hito 2, `put` y `get` hablan con dos
tipos de nodo: el ControlNode para los metadatos y los DataNodes para los
datos. Sigue el contrato fijado en `CONTRATOS.md`
(raíz del repo del servidor).

## Instalación

```bash
cd cli
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .        # habilita el comando `dfsha` en el PATH
```

## Uso

```bash
# Clúster del Hito 3 (dos ControlNodes con TLS); con un solo servidor sin
# TLS basta `dfsha login mariana` (http://localhost:8000 por defecto)
dfsha login mariana -u https://localhost:8000,https://localhost:8010 --ca-cert certs/ca.crt
dfsha whoami
dfsha pwd

dfsha mkdir /docs
dfsha mkdir -p /docs/2026/informes    # crea los intermedios que falten
dfsha cd /docs
dfsha ls .

dfsha put informe.pdf informe.pdf     # sube ./informe.pdf a /docs/informe.pdf, por bloques
dfsha put -b 65536 video.bin          # con bloques de 64 KiB
dfsha get informe.pdf copia.pdf       # descarga /docs/informe.pdf a ./copia.pdf
dfsha nodes                           # DataNodes del clúster y su estado
dfsha stat informe.pdf                # tamaño, dueño y fechas, sin descargar

dfsha rm informe.pdf
dfsha cd ..
dfsha rmdir docs
dfsha rmdir -r docs                   # con su contenido

# Hito 3
dfsha status                          # líder, DataNodes, réplicas, bloques sub-replicados
dfsha chmod 750 /docs                 # permisos (octal)
dfsha group create equipo && dfsha group add equipo ana
dfsha chgrp equipo /docs              # grupo dueño
dfsha acl set user:ana:r-x /docs      # ACL; también `acl get` y `acl rm user:ana`
dfsha lock /docs/informe.pdf          # lock exclusivo (o --shared); devuelve un id
dfsha put --lock <id> informe.pdf /docs/informe.pdf
dfsha unlock /docs/informe.pdf <id>
dfsha 2fa setup && dfsha 2fa enable <código>   # 2FA: el próximo login pide el código
dfsha apikey create scripts           # API key; luego `dfsha login --api-key`

dfsha logout
```

Variables de entorno útiles:

- `DFSHA_BASE_URL`: URL base por defecto del servidor, o varias separadas por comas (líder y respaldos).
- `DFSHA_CA_CERT`: CA para verificar el TLS del clúster (`certs/ca.crt`).
- `DFSHA_CONFIG_DIR`: dónde vive la sesión local (por defecto `~/.dfsha`). Útil para tests o para tener varias sesiones en paralelo.
- `DFSHA_BLOCK_SIZE`: tamaño de bloque que pide `put` (por defecto, el del ControlNode: 8 MiB).
- `DFSHA_PARALLEL_TRANSFERS`: cuántos bloques se suben o bajan a la vez (por defecto 4).

## Estado de la integración

| Comando(s)                  | Habla con          | Estado                    |
|------------------------------|---------------------|---------------------------|
| `login`, `logout`, `whoami`, `pwd` | ControlNode (`/auth`) | ✅ |
| `ls`, `mkdir`, `rmdir`, `rm`, `stat`, `cd` | ControlNode (`/fs/*`) | ✅ |
| `put`, `get`                  | ControlNode (`allocate`/`confirm`/`blocks`) + DataNodes (`/blocks/*`) | ✅ probado end-to-end contra `docker compose` |
| `nodes`, `status`             | ControlNode (`/datanodes`, `/cluster/status`) | ✅ |
| `chmod`, `chgrp`, `acl`, `group` | ControlNode (`/fs/*`, `/groups`) | ✅ |
| `lock`, `unlock`, `locks`     | ControlNode (`/files/*/lock`) | ✅ |
| `2fa`, `apikey`, `login --api-key` | ControlNode (`/auth/*`) | ✅ |

Si un endpoint no existiera en el servidor, la CLI muestra un mensaje
explícito ("endpoint aún no implementado en el servidor") en vez de un
error genérico, y `cd` degrada a actualizar solo el directorio de trabajo
local.

## Decisiones de diseño (por si preguntan en la sustentación)

- **Login con JWT**: `dfsha login <usuario>` pide la contraseña, la manda
  a `POST /auth/login` y guarda el token devuelto en
  `~/.dfsha/session.json` (permisos 600). Todas las peticiones siguientes
  viajan con `Authorization: Bearer <token>`. Toda la lógica de auth vive
  aislada en `auth.py` (`login` + `build_auth_headers`): ni `client.py` ni
  los comandos de `app.py` saben cómo se autentica. El token caduca (12h
  por defecto), y cuando eso pasa la API responde 401 y basta con volver a
  hacer `login`.
- **`cd` es 100% del lado del cliente**: `CONTRATOS.md` dice explícitamente
  que para el Hito 1 monolítico la CLI debe mandar siempre rutas
  absolutas y no usar `cwd_id`. Por eso el "directorio de trabajo" se
  guarda en la sesión local (`~/.dfsha/session.json`) y toda ruta
  relativa que el usuario escriba se resuelve a absoluta *antes* de
  llamar a la API (ver `pathutils.py`). Lo único que sí se consulta al
  servidor es que el destino exista y sea un directorio, vía
  `GET /fs/stat`.
- **`put`/`get` distribuidos** (`distributed.py`): `put` pide el plan a
  `allocate`, sube cada bloque **en paralelo** al primer DataNode de su
  cadena de réplica (que lo reenvía a los demás; si no responde, prueba el
  siguiente) y cierra con `confirm`, reintentando mientras los DataNodes
  informan. `get` pide `blocks` y baja cada bloque de la primera réplica que
  funcione, escribiéndolo en su offset. El usuario no ve dónde cae cada
  bloque (transparencia de localización); `put` solo informa el reparto.
- **Tokens de bloque** (Hito 3): el ControlNode firma un token por bloque
  tras comprobar los permisos; la CLI lo manda al DataNode. Al ControlNode
  le manda el JWT o la API key de la sesión.
- **Failover transparente** (Hito 3): con varios ControlNodes en la sesión,
  cada petición va al líder; si no responde o contesta `503 not_leader`,
  la CLI prueba el siguiente.
- **Lease de escritura** (Hito 3): `allocate` devuelve un lease exclusivo
  que la CLI renueva durante subidas largas y manda en `confirm`.
- **Streaming**: nunca se carga el archivo ni un bloque completo en
  memoria; todo va en chunks de 64 KiB.
- **Integridad por bloque**: `put` manda el SHA-256 de cada bloque en
  `X-Checksum-Sha256` (el DataNode rechaza lo que no coincida) y `get`
  verifica cada bloque contra el checksum del plan. Si algo falla, `get`
  no deja un archivo a medias ni pisa el archivo local previo (escribe a
  un temporal y renombra al final), y `put` no confirma.
- **Compatibilidad con el Hito 1**: un archivo sin bloques pero con
  tamaño se subió con el `PUT` monolítico, y `get` lo descarga por
  `GET /files/{ruta}` verificando el checksum del servidor.

## Tests

```bash
pytest -q
```

Cubren la resolución de rutas (`pathutils.py`), la sesión y el login
(`auth.py`) y el put/get distribuido (`distributed.py`) contra un
ControlNode y unos DataNodes falsos en memoria: reparto, propagación del
JWT, archivos grandes y vacíos, bloques corruptos o faltantes, y nodos
caídos. La prueba contra el clúster real está en
`scripts/verify_hito2.py` (raíz del repo).
