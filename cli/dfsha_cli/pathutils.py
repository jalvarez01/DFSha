"""
Resolución de rutas del lado del CLIENTE.

Por qué existe este módulo (y no simplemente mandar cwd_id al servidor):
CONTRATOS.md es explícito en que, para el Hito 1 monolítico, "lo más
simple es que la CLI siempre mande rutas absolutas y nadie necesite
preocuparse por cwd_id". Eso significa que el estado de `cd` (cuál es el
directorio de trabajo actual) vive ÚNICAMENTE en el cliente: cada vez que
el usuario da una ruta relativa (`ls docs`, `cat ../otro/archivo`), la
CLI la convierte a una ruta absoluta ANTES de llamar a la API, combinando
esa ruta con el `cwd` guardado en la sesión local.

La lógica de normalización (manejo de ".", "..", slashes repetidos) es
deliberadamente un espejo simplificado de `app/path_service.normalize_path`
del servidor, para que el comportamiento de `cd`/rutas relativas en el
cliente sea consistente con cómo el servidor interpretaría esas mismas
rutas si alguna vez decidiera aceptar rutas relativas (no lo hace hoy,
pero mantenemos la misma semántica tipo Linux por las dudas y por
claridad para el usuario).
"""
from __future__ import annotations


def normalize_absolute(path: str) -> str:
    """Normaliza una ruta ABSOLUTA (debe empezar con '/'): colapsa
    slashes repetidos y resuelve '.' y '..' de forma puramente textual,
    igual que en Linux (".." en la raíz se queda en la raíz).

    Ejemplos:
        "/a//b/./c/.." -> "/a/b"
        "/"            -> "/"
        "/../../a"     -> "/a"
    """
    if not path.startswith("/"):
        raise ValueError(f"normalize_absolute espera una ruta absoluta, recibió: {path!r}")

    segments = [seg for seg in path.split("/") if seg not in ("", ".")]
    stack: list[str] = []
    for seg in segments:
        if seg == "..":
            if stack:
                stack.pop()
            # ".." en la raíz: no-op (igual que en Linux con "/..").
        else:
            stack.append(seg)
    return "/" + "/".join(stack)


def resolve_path(cwd: str, path: str) -> str:
    """Resuelve `path` (absoluto o relativo) contra `cwd` (siempre
    absoluto) y devuelve una ruta ABSOLUTA normalizada, lista para
    mandarse al servidor.

    - Si `path` empieza con "/": se trata como absoluta y se normaliza
      directamente (se ignora `cwd`).
    - Si no: se concatena a `cwd` antes de normalizar (ruta relativa
      normal, tipo shell de Linux).
    - Vacía o "." significa "quedarse en el mismo directorio" -> cwd.
    """
    if path == "" or path == ".":
        return normalize_absolute(cwd)
    if path.startswith("/"):
        return normalize_absolute(path)
    combined = cwd.rstrip("/") + "/" + path
    return normalize_absolute(combined)


def leaf_name(path: str) -> str:
    """Devuelve el último segmento de una ruta absoluta normalizada
    (usado para mostrar mensajes amigables, p. ej. "creado directorio
    'docs'" en vez de repetir la ruta completa)."""
    normalized = normalize_absolute(path)
    if normalized == "/":
        return "/"
    return normalized.rsplit("/", 1)[-1]
