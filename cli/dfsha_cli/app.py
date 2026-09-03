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
    dfsha put <local> [remota]  Sube un archivo                (RF2)
    dfsha get <remota> [local]  Descarga un archivo            (RF2)

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
from dfsha_cli.config import DEFAULT_BASE_URL

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
    username: str = typer.Argument(..., help="Nombre de usuario"),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL, "--base-url", "-u", help="URL base del servidor DFSha"
    ),
):
    """Inicia sesión contra el servidor DFSha y guarda la sesión localmente.

    Nota: en el Hito 1 el servidor todavía no valida contraseñas (no hay
    login real / JWT); se pide la contraseña para que el flujo de usuario
    ya quede definitivo, pero solo se verifica que el servidor responda.
    """
    password = typer.prompt("Contraseña", hide_input=True)
    try:
        session = auth.login(username=username, password=password, base_url=base_url)
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
    table.add_column("Tipo")
    table.add_column("Nombre")
    table.add_column("Tamaño", justify="right")
    table.add_column("Creado")
    for entry in data.get("entries", []):
        kind = "dir" if entry["type"] == "directory" else "file"
        size = "-" if entry["type"] == "directory" else str(entry["size_bytes"])
        table.add_row(kind, entry["name"], size, str(entry["created_at"]))
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


@app.command()
@_handle_errors
def put(
    local_path: Path = typer.Argument(..., help="Archivo local a subir", exists=True, readable=True, dir_okay=False),
    remote_path: Optional[str] = typer.Argument(
        None, help="Ruta remota destino (por defecto: mismo nombre, en el cwd actual)"
    ),
):
    """Sube (send) un archivo local al servidor."""
    client, session = _load_client_and_session()
    target = _resolve(session, remote_path if remote_path else local_path.name)

    size = local_path.stat().st_size
    with console.status(f"Subiendo {local_path.name} ({size} bytes) a {target}..."):
        data = client.put_file(target, local_path)

    verb = "creado" if data.get("created") else "sobreescrito"
    console.print(f"[green]✓[/green] Archivo {verb}: {data['file']['path']} ({data['file']['size_bytes']} bytes)")


@app.command()
@_handle_errors
def get(
    remote_path: str = typer.Argument(..., help="Ruta remota a descargar"),
    local_path: Optional[Path] = typer.Argument(
        None, help="Archivo local destino (por defecto: mismo nombre, en el directorio actual)"
    ),
):
    """Descarga (receive) un archivo remoto al disco local."""
    client, session = _load_client_and_session()
    target = _resolve(session, remote_path)
    dest = local_path if local_path is not None else Path(pathutils.leaf_name(target))

    with console.status(f"Descargando {target} a {dest}..."):
        meta = client.get_file(target, dest)

    console.print(f"[green]✓[/green] Descargado en {dest} ({dest.stat().st_size} bytes)")

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


def main():
    app()


if __name__ == "__main__":
    main()
