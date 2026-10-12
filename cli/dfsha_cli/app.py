"""
CLI de DFSha (Typer).

Comandos implementados (según el reparto de tareas del equipo):
    dfsha login <usuario>      Inicia sesión (guarda sesión local)
    dfsha logout               Cierra la sesión local
    dfsha whoami                Muestra el usuario/sesión actual
    dfsha pwd                   Muestra el directorio de trabajo actual
    dfsha ls [ruta]              Lista un directorio          (RF1)
    dfsha cd <ruta>              Cambia el directorio de trabajo (RF1, cliente)
    dfsha stat [ruta]            Metadatos de un archivo/directorio (RF1)
    dfsha mkdir <ruta> [-p]      Crea un directorio            (RF1)
    dfsha rmdir <ruta> [-r]      Borra un directorio           (RF1)
    dfsha rm <ruta>              Borra un archivo               (RF1)
    dfsha put <local> [remota]  Sube un archivo por bloques     (RF2, Hito 2)
    dfsha get <remota> [local]  Descarga un archivo por bloques (RF2, Hito 2)
    dfsha nodes                 Lista los DataNodes del clúster  (Hito 2)
    dfsha status                Estado del clúster: líder, réplicas (Hito 3)
    dfsha chmod/chgrp/acl ...   Permisos, grupos dueños y ACL      (Hito 3)
    dfsha group ...             Grupos de usuarios                 (Hito 3)
    dfsha lock/unlock/locks     Locks de archivo (RF3 lock())      (Hito 3)
    dfsha 2fa ... / apikey ...  2FA (TOTP) y API keys              (Hito 3)

put/get hablan con dos tipos de nodo: el ControlNode para el plan de
bloques y cada DataNode para los datos (ver distributed.py). El usuario no
necesita saber dónde cae cada bloque.

Todas las rutas remotas que da el usuario pueden ser absolutas o
relativas al cwd guardado en la sesión (ver pathutils.resolve_path); la
CLI siempre las convierte a absolutas antes de llamar a la API, tal como
exige CONTRATOS.md.
"""
from __future__ import annotations

import hashlib
import sys
from functools import wraps
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from dfsha_cli import auth, pathutils
from dfsha_cli.client import APIError, ConnectionFailed, DFShaClient, RouteNotImplemented
from dfsha_cli.config import DEFAULT_BASE_URL, DEFAULT_BLOCK_SIZE, DEFAULT_CA_CERT
from dfsha_cli.distributed import BlockTransferError, DistributedTransfer, TransferSummary

app = typer.Typer(
    name="dfsha",
    help="Cliente CLI de DFSha: gestión de archivos y transferencia contra el servidor DFSha.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True, style="bold red")


def _handle_errors(func):
    """Decorador para todos los comandos: centraliza el manejo de
    errores (sesión inexistente, error de negocio del servidor, error de
    conexión, endpoint aún no implementado) en un solo lugar, con
    mensajes en español y exit code 1."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except auth.NotLoggedInError as exc:
            err_console.print(f"✗ {exc}")
            raise typer.Exit(code=1)
        except RouteNotImplemented as exc:
            err_console.print(f"✗ Funcionalidad aún no disponible en el servidor: {exc}")
            raise typer.Exit(code=2)
        except ConnectionFailed as exc:
            err_console.print(f"✗ {exc}")
            raise typer.Exit(code=1)
        except APIError as exc:
            err_console.print(f"✗ {exc}")
            raise typer.Exit(code=1)
        except BlockTransferError as exc:
            err_console.print(f"✗ Falló la transferencia con un DataNode: {exc}")
            raise typer.Exit(code=1)
        except FileNotFoundError as exc:
            err_console.print(f"✗ Archivo local no encontrado: {exc.filename}")
            raise typer.Exit(code=1)

    return wrapper


def _load_client_and_session() -> tuple[DFShaClient, auth.Session]:
    session = auth.load_session()
    return DFShaClient(session), session


def _resolve(session: auth.Session, path: str) -> str:
    return pathutils.resolve_path(session.cwd, path)


# --------------------------------------------------------------------------
# Autenticación / sesión
# --------------------------------------------------------------------------
@app.command()
@_handle_errors
def login(
    username: Optional[str] = typer.Argument(None, help="Nombre de usuario (no hace falta con --api-key)"),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL,
        "--base-url",
        "-u",
        help="URL del ControlNode, o varias separadas por comas (líder y respaldos)",
    ),
    api_key: bool = typer.Option(False, "--api-key", help="Iniciar sesión con una API key en vez de contraseña"),
    ca_cert: Optional[str] = typer.Option(
        DEFAULT_CA_CERT or None, "--ca-cert", help="CA para verificar el TLS del clúster"
    ),
):
    """Inicia sesión contra el servidor DFSha (usuario/contraseña, y código
    2FA si lo tiene activado) y guarda el JWT en ~/.dfsha/session.json.

    Si el usuario no tiene contraseña todavía, este primer login la fija.
    """
    try:
        if api_key:
            key = typer.prompt("API key", hide_input=True)
            session = auth.login_with_api_key(key, base_url=base_url, ca_cert=ca_cert)
        else:
            if not username:
                err_console.print("✗ Falta el nombre de usuario")
                raise typer.Exit(code=1)
            password = typer.prompt("Contraseña", hide_input=True)
            try:
                session = auth.login(username=username, password=password, base_url=base_url, ca_cert=ca_cert)
            except auth.OtpRequired:
                code = typer.prompt("Código 2FA")
                session = auth.login(
                    username=username, password=password, base_url=base_url, otp=code, ca_cert=ca_cert
                )
    except auth.LoginError as exc:
        err_console.print(f"✗ {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] Sesión iniciada como [bold]{session.username}[/bold] en {session.base_url}")


@app.command()
@_handle_errors
def logout():
    """Cierra la sesión local (borra el archivo de sesión)."""
    if auth.clear_session():
        console.print("[green]✓[/green] Sesión cerrada.")
    else:
        console.print("No había ninguna sesión activa.")


@app.command()
@_handle_errors
def whoami():
    """Muestra el usuario y servidor de la sesión activa."""
    session = auth.load_session()
    console.print(f"usuario: [bold]{session.username}[/bold]")
    console.print(f"servidor: {session.base_url}")
    console.print(f"cwd: {session.cwd}")


@app.command()
@_handle_errors
def pwd():
    """Muestra el directorio de trabajo actual (estado local de 'cd')."""
    session = auth.load_session()
    console.print(session.cwd)


# --------------------------------------------------------------------------
# RF1: gestión del filesystem
# --------------------------------------------------------------------------
@app.command()
@_handle_errors
def ls(path: str = typer.Argument(".", help="Ruta a listar (absoluta o relativa al cwd)")):
    """Lista el contenido de un directorio remoto."""
    client, session = _load_client_and_session()
    target = _resolve(session, path)
    data = client.ls(target)

    table = Table(title=data.get("path", target))
    table.add_column("Permisos")
    table.add_column("Dueño")
    table.add_column("Grupo")
    table.add_column("Nombre")
    table.add_column("Tamaño", justify="right")
    table.add_column("Creado")
    for entry in data.get("entries", []):
        size = "-" if entry["type"] == "directory" else str(entry["size_bytes"])
        name = f"[bold blue]{entry['name']}/[/bold blue]" if entry["type"] == "directory" else entry["name"]
        table.add_row(
            entry.get("mode") or "", entry.get("owner") or "", entry.get("group") or "-", name, size, str(entry["created_at"])
        )
    console.print(table)


@app.command()
@_handle_errors
def cd(path: str = typer.Argument(..., help="Ruta a la que moverse (absoluta o relativa)")):
    """Cambia el directorio de trabajo actual (estado guardado en el cliente).

    Valida contra el servidor que la ruta exista y sea un directorio; si el
    endpoint /fs/stat todavía no está disponible, avisa pero igual actualiza
    el cwd local (para no bloquear a quien esté probando el resto de la CLI
    mientras ese endpoint no exista).
    """
    session = auth.load_session()
    target = pathutils.resolve_path(session.cwd, path)

    client = DFShaClient(session)
    try:
        entry = client.stat(target)
    except RouteNotImplemented:
        console.print(
            "[yellow]![/yellow] No se pudo validar contra el servidor "
            "(endpoint /fs/stat aún no implementado); cwd actualizado solo localmente."
        )
    except APIError as exc:
        err_console.print(f"✗ No se pudo cambiar a {target}: {exc}")
        raise typer.Exit(code=1)
    else:
        # stat resuelve archivos y directorios por igual, así que el tipo hay
        # que comprobarlo aquí: 'cd archivo.txt' no debe cambiar el cwd.
        if entry.get("type") != "directory":
            err_console.print(f"✗ No se pudo cambiar a {target}: no es un directorio")
            raise typer.Exit(code=1)

    session.cwd = target
    auth.save_session(session)
    console.print(session.cwd)


@app.command()
@_handle_errors
def stat(path: str = typer.Argument(".", help="Ruta a consultar (archivo o directorio)")):
    """Muestra los metadatos de un archivo o directorio remoto."""
    client, session = _load_client_and_session()
    target = _resolve(session, path)
    data = client.stat(target)

    table = Table(title=data["path"])
    table.add_column("Campo")
    table.add_column("Valor")
    table.add_row("tipo", "dir" if data["type"] == "directory" else "file")
    table.add_row("nombre", data["name"])
    table.add_row("tamaño", "-" if data["type"] == "directory" else f"{data['size_bytes']} bytes")
    table.add_row("dueño", data["owner"])
    table.add_row("grupo", data.get("group") or "-")
    if data.get("mode"):
        table.add_row("permisos", f"{data['mode']} ({data.get('mode_octal')})")
    table.add_row("creado", str(data["created_at"]))
    if data.get("updated_at"):
        table.add_row("modificado", str(data["updated_at"]))
    console.print(table)


@app.command()
@_handle_errors
def mkdir(
    path: str = typer.Argument(..., help="Ruta del directorio a crear"),
    parents: bool = typer.Option(
        False,
        "--parents",
        "-p",
        help="Crear los directorios intermedios que falten y no fallar si ya existe",
    ),
):
    """Crea un directorio remoto (sin -p, el padre debe existir)."""
    client, session = _load_client_and_session()
    target = _resolve(session, path)
    data = client.mkdir(target, parents=parents)
    console.print(f"[green]✓[/green] Directorio creado: {data['path']}")


@app.command()
@_handle_errors
def rmdir(
    path: str = typer.Argument(..., help="Ruta del directorio a borrar"),
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Borrar aunque el directorio no esté vacío"
    ),
):
    """Borra un directorio remoto."""
    client, session = _load_client_and_session()
    target = _resolve(session, path)
    data = client.rmdir(target, recursive=recursive)
    console.print(f"[green]✓[/green] Directorio borrado: {data['path']}")


@app.command()
@_handle_errors
def rm(path: str = typer.Argument(..., help="Ruta del archivo a borrar")):
    """Borra un archivo remoto."""
    client, session = _load_client_and_session()
    target = _resolve(session, path)
    data = client.rm(target)
    console.print(f"[green]✓[/green] Archivo borrado: {data['path']}")


# --------------------------------------------------------------------------
# RF2: transferencia de archivos
# --------------------------------------------------------------------------
def _sha256_of(local_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(local_path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _describe_nodes(summary: TransferSummary) -> str:
    if not summary.blocks_per_node:
        return "ninguno (archivo vacío)"
    return ", ".join(f"{node}×{count}" for node, count in sorted(summary.blocks_per_node.items()))


@app.command()
@_handle_errors
def put(
    local_path: Path = typer.Argument(..., help="Archivo local a subir", exists=True, readable=True, dir_okay=False),
    remote_path: Optional[str] = typer.Argument(
        None, help="Ruta remota destino (por defecto: mismo nombre, en el cwd actual)"
    ),
    block_size: Optional[int] = typer.Option(
        DEFAULT_BLOCK_SIZE,
        "--block-size",
        "-b",
        min=1,
        help="Tamaño de bloque en bytes (por defecto, el del ControlNode: 8 MiB)",
    ),
    lease_id: Optional[str] = typer.Option(
        None, "--lock", help="Id de un lock exclusivo que ya tienes sobre el archivo (dfsha lock)"
    ),
):
    """Sube (send) un archivo: lo parte en bloques y los sube en paralelo a
    los DataNodes que asigne el ControlNode."""
    client, session = _load_client_and_session()
    target = _resolve(session, remote_path if remote_path else local_path.name)

    size = local_path.stat().st_size
    with console.status(f"Subiendo {local_path.name} ({size} bytes) a {target}..."):
        with DistributedTransfer(client) as transfer:
            summary = transfer.put(target, local_path, block_size=block_size, lease_id=lease_id)

    console.print(f"[green]✓[/green] Archivo subido: {summary.path} ({summary.size_bytes} bytes)")
    console.print(f"  {summary.num_blocks} bloque(s) × {summary.replication} réplica(s): {_describe_nodes(summary)}")
    if summary.failovers:
        console.print(f"  [yellow]![/yellow] {summary.failovers} reintento(s) en otro DataNode de la cadena")
    if not summary.complete:
        err_console.print("✗ El ControlNode no dio el archivo por completo; vuelve a subirlo.")
        raise typer.Exit(code=1)


@app.command()
@_handle_errors
def get(
    remote_path: str = typer.Argument(..., help="Ruta remota a descargar"),
    local_path: Optional[Path] = typer.Argument(
        None, help="Archivo local destino (por defecto: mismo nombre, en el directorio actual)"
    ),
):
    """Descarga (receive) un archivo: baja sus bloques en paralelo desde los
    DataNodes, verifica el checksum de cada uno y los reensambla en orden."""
    client, session = _load_client_and_session()
    target = _resolve(session, remote_path)
    dest = local_path if local_path is not None else Path(pathutils.leaf_name(target))

    with console.status(f"Descargando {target} a {dest}..."):
        plan = client.blocks(target)
        if plan["num_blocks"] == 0 and client.stat(target)["size_bytes"] > 0:
            # Archivo subido con el PUT monolítico del Hito 1: su contenido
            # no está en bloques sino en el servidor.
            _get_legacy(client, target, dest)
            return
        with DistributedTransfer(client) as transfer:
            summary = transfer.get(target, dest)

    console.print(f"[green]✓[/green] Descargado en {dest} ({summary.size_bytes} bytes)")
    console.print(f"  {summary.num_blocks} bloque(s) desde {_describe_nodes(summary)}, checksum de cada bloque verificado")
    if summary.failovers:
        console.print(f"  [yellow]![/yellow] {summary.failovers} réplica(s) no respondieron; se usó otra")


def _get_legacy(client: DFShaClient, target: str, dest: Path) -> None:
    meta = client.get_file(target, dest)
    console.print(f"[green]✓[/green] Descargado en {dest} ({dest.stat().st_size} bytes) \\[formato Hito 1]")

    expected_checksum = meta.get("checksum_sha256")
    if expected_checksum:
        actual_checksum = _sha256_of(dest)
        if actual_checksum == expected_checksum:
            console.print(f"[green]✓[/green] Checksum verificado (sha256={actual_checksum[:12]}...)")
        else:
            err_console.print(
                f"✗ ¡Checksum no coincide! esperado={expected_checksum} obtenido={actual_checksum}. "
                f"El archivo pudo corromperse en la descarga."
            )
            raise typer.Exit(code=1)


@app.command()
@_handle_errors
def nodes():
    """Lista los DataNodes registrados en el ControlNode y su estado."""
    client, _ = _load_client_and_session()
    table = Table(title="DataNodes")
    table.add_column("Nodo")
    table.add_column("Dirección")
    table.add_column("Estado")
    table.add_column("Último heartbeat")
    for node in client.datanodes():
        status = "[green]vivo[/green]" if node["status"] == "alive" else "[red]caído[/red]"
        table.add_row(node["node_id"], f"{node['host']}:{node['port']}", status, str(node["last_heartbeat"]))
    console.print(table)


# --------------------------------------------------------------------------
# Hito 3: estado del clúster
# --------------------------------------------------------------------------
@app.command()
@_handle_errors
def status():
    """Estado del clúster: ControlNode líder, DataNodes y réplicas."""
    client, _ = _load_client_and_session()
    data = client.cluster_status()
    cn = data["controlnode"]
    console.print(
        f"ControlNode [bold]{cn['controlnode_id']}[/bold]: {cn['role']} (época {cn['epoch']}) en {client.base_url}"
    )
    for peer in cn.get("peers", []):
        console.print(f"  respaldo {peer['follower']}: log confirmado hasta {peer['acked_seq']}")
    table = Table(title=f"DataNodes (factor de replicación {data['replication_factor']})")
    table.add_column("Nodo")
    table.add_column("Dirección")
    table.add_column("Estado")
    table.add_column("Réplicas", justify="right")
    for node in data["datanodes"]:
        state = "[green]vivo[/green]" if node["status"] == "alive" else "[red]caído[/red]"
        table.add_row(node["node_id"], node["address"], state, str(node["blocks"]))
    console.print(table)
    console.print(f"{data['files']} archivo(s), {data['blocks']} bloque(s)")
    color = "green" if not data["under_replicated"] and not data["missing"] else "yellow"
    console.print(
        f"[{color}]sub-replicados: {data['under_replicated']} · sin réplica viva: {data['missing']}[/{color}]"
        f" · copias en curso: {data['pending_replications']} · borrados pendientes: {data['pending_deletions']}"
    )


# --------------------------------------------------------------------------
# Hito 3: permisos, grupos y ACL
# --------------------------------------------------------------------------
def _print_acl(data: dict) -> None:
    console.print(f"# archivo: {data['path']}")
    console.print(f"# dueño: {data['owner']}")
    console.print(f"# grupo: {data['group'] or '-'}")
    console.print(f"{data['mode']}  ({data['mode_octal']})")
    for entry in data["entries"]:
        console.print(f"{entry['type']}:{entry['name']}:{entry['perms']}")


@app.command()
@_handle_errors
def chmod(
    mode: str = typer.Argument(..., help="Modo en octal, p. ej. 750 o 1777"),
    path: str = typer.Argument(..., help="Archivo o directorio"),
):
    """Cambia los permisos (solo el dueño)."""
    client, session = _load_client_and_session()
    _print_acl(client.chmod(_resolve(session, path), mode))


@app.command()
@_handle_errors
def chgrp(
    group: str = typer.Argument(..., help="Grupo (o '-' para quitarlo)"),
    path: str = typer.Argument(..., help="Archivo o directorio"),
):
    """Cambia el grupo dueño (solo el dueño, y de un grupo del que seas miembro)."""
    client, session = _load_client_and_session()
    _print_acl(client.chgrp(_resolve(session, path), None if group == "-" else group))


acl_app = typer.Typer(help="ACL: permisos para usuarios o grupos concretos")
app.add_typer(acl_app, name="acl")


@acl_app.command("get")
@_handle_errors
def acl_get(path: str = typer.Argument(..., help="Archivo o directorio")):
    """Muestra dueño, grupo, modo y entradas de ACL."""
    client, session = _load_client_and_session()
    _print_acl(client.get_acl(_resolve(session, path)))


@acl_app.command("set")
@_handle_errors
def acl_set(
    entry: str = typer.Argument(..., help="user:<nombre>:<rwx> o group:<nombre>:<rwx>, p. ej. user:ana:r-x"),
    path: str = typer.Argument(..., help="Archivo o directorio"),
):
    """Añade o cambia una entrada de ACL (como setfacl -m)."""
    try:
        kind, name, perms = entry.split(":")
    except ValueError:
        err_console.print("✗ Formato: user:<nombre>:<rwx> o group:<nombre>:<rwx>")
        raise typer.Exit(code=1)
    client, session = _load_client_and_session()
    _print_acl(client.set_acl(_resolve(session, path), kind, name, perms))


@acl_app.command("rm")
@_handle_errors
def acl_rm(
    entry: str = typer.Argument(..., help="user:<nombre> o group:<nombre>"),
    path: str = typer.Argument(..., help="Archivo o directorio"),
):
    """Quita una entrada de ACL (como setfacl -x)."""
    try:
        kind, name = entry.split(":")
    except ValueError:
        err_console.print("✗ Formato: user:<nombre> o group:<nombre>")
        raise typer.Exit(code=1)
    client, session = _load_client_and_session()
    _print_acl(client.remove_acl(_resolve(session, path), kind, name))


group_app = typer.Typer(help="Grupos de usuarios")
app.add_typer(group_app, name="group")


def _print_group(data: dict) -> None:
    console.print(f"[bold]{data['name']}[/bold] (dueño {data['owner']}): {', '.join(data['members'])}")


@group_app.command("create")
@_handle_errors
def group_create(name: str):
    """Crea un grupo (quedas como dueño y miembro)."""
    client, _ = _load_client_and_session()
    _print_group(client.create_group(name))


@group_app.command("add")
@_handle_errors
def group_add(name: str, username: str):
    """Añade un usuario a un grupo tuyo."""
    client, _ = _load_client_and_session()
    _print_group(client.add_group_member(name, username))


@group_app.command("remove")
@_handle_errors
def group_remove(name: str, username: str):
    """Quita un usuario de un grupo tuyo."""
    client, _ = _load_client_and_session()
    _print_group(client.remove_group_member(name, username))


@group_app.command("list")
@_handle_errors
def group_list():
    """Grupos de los que eres miembro."""
    client, _ = _load_client_and_session()
    groups = client.list_groups()
    if not groups:
        console.print("No perteneces a ningún grupo.")
    for data in groups:
        _print_group(data)


@group_app.command("delete")
@_handle_errors
def group_delete(name: str):
    """Borra un grupo tuyo."""
    client, _ = _load_client_and_session()
    client.delete_group(name)
    console.print(f"[green]✓[/green] Grupo {name} borrado")


# --------------------------------------------------------------------------
# Hito 3: locks (RF3 lock())
# --------------------------------------------------------------------------
@app.command()
@_handle_errors
def lock(
    path: str = typer.Argument(..., help="Archivo a bloquear"),
    shared: bool = typer.Option(False, "--shared", "-s", help="Lock compartido (lectura) en vez de exclusivo"),
    ttl: Optional[int] = typer.Option(None, "--ttl", help="Duración en segundos"),
):
    """Toma un lock sobre un archivo. Con uno exclusivo, nadie más puede
    escribirlo; para escribir tú, pásalo a `dfsha put --lock <id>`."""
    client, session = _load_client_and_session()
    data = client.lock(_resolve(session, path), "shared" if shared else "exclusive", ttl)
    console.print(f"[green]✓[/green] Lock {data['mode']} {data['lock_id']} sobre {data['path']} hasta {data['expires_at']}")


@app.command()
@_handle_errors
def unlock(path: str = typer.Argument(...), lock_id: str = typer.Argument(...)):
    """Libera un lock tuyo."""
    client, session = _load_client_and_session()
    client.unlock(_resolve(session, path), lock_id)
    console.print(f"[green]✓[/green] Lock {lock_id} liberado")


@app.command()
@_handle_errors
def locks(path: str = typer.Argument(...)):
    """Locks vigentes sobre un archivo."""
    client, session = _load_client_and_session()
    entries = client.locks(_resolve(session, path))
    if not entries:
        console.print("Sin locks vigentes.")
    for data in entries:
        kind = "lease de escritura" if data["implicit"] else data["mode"]
        console.print(f"{data['lock_id']}  {kind}  de {data['owner']}  hasta {data['expires_at']}")


# --------------------------------------------------------------------------
# Hito 3: 2FA y API keys
# --------------------------------------------------------------------------
twofa_app = typer.Typer(help="Autenticación en dos pasos (TOTP)")
app.add_typer(twofa_app, name="2fa")


@twofa_app.command("status")
@_handle_errors
def twofa_status():
    client, _ = _load_client_and_session()
    data = client.two_factor_status()
    console.print(f"2FA para {data['username']}: {'activado' if data['enabled'] else 'desactivado'}")


@twofa_app.command("setup")
@_handle_errors
def twofa_setup():
    """Genera el secreto TOTP. Agrégalo a tu app de autenticación y luego
    activa con `dfsha 2fa enable <código>`."""
    client, _ = _load_client_and_session()
    data = client.two_factor_setup()
    console.print(f"Secreto: [bold]{data['secret']}[/bold]")
    console.print(f"URI (para generar un QR): {data['otpauth_uri']}")
    console.print("Confirma con: dfsha 2fa enable <código de 6 dígitos>")


@twofa_app.command("enable")
@_handle_errors
def twofa_enable(code: str):
    client, _ = _load_client_and_session()
    client.two_factor_enable(code)
    console.print("[green]✓[/green] 2FA activado: el próximo login pedirá el código")


@twofa_app.command("disable")
@_handle_errors
def twofa_disable(code: str):
    client, _ = _load_client_and_session()
    client.two_factor_disable(code)
    console.print("[green]✓[/green] 2FA desactivado")


apikey_app = typer.Typer(help="API keys para scripts")
app.add_typer(apikey_app, name="apikey")


@apikey_app.command("create")
@_handle_errors
def apikey_create(
    name: str,
    days: Optional[int] = typer.Option(None, "--days", help="Caduca en N días"),
):
    client, _ = _load_client_and_session()
    data = client.create_api_key(name, days)
    console.print(f"[green]✓[/green] API key {data['id']} ({data['name']}). Guárdala, no se vuelve a mostrar:")
    console.print(f"[bold]{data['key']}[/bold]")


@apikey_app.command("list")
@_handle_errors
def apikey_list():
    client, _ = _load_client_and_session()
    for key in client.list_api_keys():
        state = "revocada" if key["revoked"] else "activa"
        console.print(
            f"{key['id']}  {key['name']}  dfsha_{key['prefix']}_…  {state}  último uso: {key['last_used_at'] or '-'}"
        )


@apikey_app.command("revoke")
@_handle_errors
def apikey_revoke(key_id: int):
    client, _ = _load_client_and_session()
    client.revoke_api_key(key_id)
    console.print(f"[green]✓[/green] API key {key_id} revocada")


def main():
    app()


if __name__ == "__main__":
    main()
