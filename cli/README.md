# dfsha-cli

Cliente CLI de DFSha (Hitos 1 y 2). Implementa la parte de **Mariana**:
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
# Por defecto apunta a http://localhost:8000; usar --base-url para otro server
dfsha login mariana
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

dfsha logout
```

Variables de entorno útiles:

- `DFSHA_BASE_URL`: URL base por defecto del servidor (si no se usa `--base-url` en `login`).
- `DFSHA_CONFIG_DIR`: dónde vive la sesión local (por defecto `~/.dfsha`). Útil para tests o para tener varias sesiones en paralelo.
- `DFSHA_BLOCK_SIZE`: tamaño de bloque que pide `put` (por defecto, el del ControlNode: 8 MiB).
- `DFSHA_PARALLEL_TRANSFERS`: cuántos bloques se suben o bajan a la vez (por defecto 4).

## Estado de la integración

| Comando(s)                  | Habla con          | Estado                    |
|------------------------------|---------------------|---------------------------|
| `login`, `logout`, `whoami`, `pwd` | ControlNode (`/auth`) | ✅ |
| `ls`, `mkdir`, `rmdir`, `rm`, `stat`, `cd` | ControlNode (`/fs/*`) | ✅ |
| `put`, `get`                  | ControlNode (`allocate`/`confirm`/`blocks`) + DataNodes (`/blocks/*`) | ✅ probado end-to-end contra `docker compose` |
| `nodes`                       | ControlNode (`/datanodes`) | ✅ |

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
  `allocate`, sube cada bloque **en paralelo** directo a su DataNode y
  cierra con `confirm`; `get` pide `blocks`, baja los bloques en paralelo
  y escribe cada uno en su offset. El usuario no ve dónde cae cada bloque
  (transparencia de localización); `put` solo informa el reparto.
- **JWT propagado**: el mismo token del login va al ControlNode y a cada
  DataNode, que lo valida con el secreto compartido.
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
