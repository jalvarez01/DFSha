# dfsha-cli

Cliente CLI de DFSha (Hito 1). Implementa la parte de **Mariana**: gestión
del filesystem y transferencia desde el lado del cliente, más
autenticación/sesión local. Sigue el contrato fijado en `CONTRATOS.md`
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

dfsha put informe.pdf informe.pdf     # sube ./informe.pdf a /docs/informe.pdf
dfsha get informe.pdf copia.pdf       # descarga /docs/informe.pdf a ./copia.pdf
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

## Estado actual de la integración con el resto del equipo

Esta CLI está escrita **contra el contrato** (`CONTRATOS.md`), no contra
el código final de cada compañero, así que hoy (Hito 1 en progreso):

| Comando(s)                  | Depende de          | Estado                    |
|------------------------------|---------------------|---------------------------|
| `login`, `logout`, `whoami`, `pwd` | Solo cliente        | ✅ funcionando |
| `put`, `get`                  | `/files/*` (Paulina) | ✅ probado end-to-end (subida, descarga, checksum, sobreescritura) |
| `ls`, `mkdir`, `rmdir`, `rm`, `stat`, `cd` (validación) | `/fs/*` (Jacobo) | ✅ probado end-to-end contra el router `/fs` ya registrado en `app/main.py` |

La detección de endpoints no registrados sigue en su sitio por si en el
futuro se agregan rutas nuevas: si un `/fs/*` no existiera, la CLI muestra
un mensaje explícito ("endpoint aún no implementado en el servidor") en vez
de un error genérico, y `cd` degrada a actualizar solo el directorio de
trabajo local.

## Decisiones de diseño (por si preguntan en la sustentación)

- **Login sin JWT real**: el servidor de este hito NO valida contraseñas
  todavía (ver `app/deps.py::get_current_user` — autenticación real es
  un placeholder). Por eso `dfsha login` pide usuario/contraseña (para
  que el flujo de usuario final ya esté listo) pero solo usa el usuario:
  la lógica de auth vive aislada en `auth.py` (`build_auth_headers`),
  así que cuando el equipo agregue un endpoint real de login con JWT,
  ese es el único archivo que hay que tocar.
- **`cd` es 100% del lado del cliente**: `CONTRATOS.md` dice explícitamente
  que para el Hito 1 monolítico la CLI debe mandar siempre rutas
  absolutas y no usar `cwd_id`. Por eso el "directorio de trabajo" se
  guarda en la sesión local (`~/.dfsha/session.json`) y toda ruta
  relativa que el usuario escriba se resuelve a absoluta *antes* de
  llamar a la API (ver `pathutils.py`). Lo único que sí se consulta al
  servidor es que el destino exista y sea un directorio, vía
  `GET /fs/stat`.
- **`put`/`get` en streaming**: nunca se carga el archivo completo en
  memoria (ni al subir ni al bajar), acorde con `RNF1` (escalabilidad en
  tamaño de archivo) y con cómo Paulina implementó el servidor
  (body crudo + `StreamingResponse`, no multipart).
- **Verificación de integridad**: tras cada `get`, se recalcula el
  SHA-256 local y se compara contra el header `X-Checksum-Sha256` que
  manda el servidor, avisando si no coinciden.

## Tests

```bash
pytest -q
```

Cubren la resolución de rutas (`pathutils.py`), que es la lógica propia
de este módulo que no depende de tener el servidor corriendo. El resto de
los comandos se probó manualmente end-to-end contra un server local (ver
tabla de arriba); los endpoints que consumen tienen su propia suite en
`tests/test_fs.py` y `tests/test_transfer.py` del repo del servidor.
