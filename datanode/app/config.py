"""Configuración del DataNode, leída de variables de entorno.

Nombres fijados en CONTRATOS.md (sección "Infraestructura") para que
`docker-compose.yml` pueda levantar varios DataNodes solo cambiando estas
variables por contenedor.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DFSHA_", extra="ignore")

    # Identidad de este DataNode frente al ControlNode. Debe ser estable
    # entre reinicios (si cambia, el ControlNode lo trata como un nodo
    # nuevo y los bloques que tenía asignados se dan por perdidos).
    datanode_id: str = "dn-local"

    # ControlNodes (Hito 3: puede haber varios, líder y respaldos). Lista
    # separada por comas; `controlnode_url` se mantiene por compatibilidad.
    controlnode_urls: str = ""
    controlnode_url: str = "http://localhost:8000"

    # host:puerto que el ControlNode anuncia a los CLIENTES para que le
    # hablen directo a este DataNode. En docker-compose es localhost y el
    # puerto publicado; uvicorn escucha en ese mismo puerto (ver Dockerfile).
    datanode_advertise_host: str = "localhost"
    datanode_advertise_port: int = 9001
    # host:puerto con el que lo alcanzan los OTROS DataNodes (cadena de
    # escritura y re-replicación). En docker-compose, el nombre del servicio.
    # Vacío = el mismo que el anunciado.
    datanode_internal_host: str = ""
    datanode_internal_port: int = 0

    # Cada cuánto se manda heartbeat a los ControlNodes.
    heartbeat_interval: float = 10.0
    # Cada cuánto se manda el inventario completo (block report).
    block_report_interval: float = 600.0

    # Dónde guarda los bloques en disco: <storage_dir>/<file_id>/<block_id>
    # ("archivo como directorio, bloque como archivo" — decisión de equipo).
    datanode_storage_dir: Path = Path("/data/blocks")

    # Hito 3 — seguridad.
    # Secreto del clúster: valida los tokens de bloque que firma el
    # ControlNode y firma los tokens con los que este nodo se identifica.
    cluster_secret: str = "dev-cluster-secret-cambiar-en-produccion"
    # Clave AES-256 (base64, 32 bytes) para cifrar los bloques en disco.
    # Vacía = sin cifrado en reposo (solo desarrollo).
    datanode_storage_key: str = ""
    # TLS: si hay certificado, este nodo sirve HTTPS (uvicorn lo carga con
    # --ssl-certfile/--ssl-keyfile) y se anuncia con esquema https.
    tls_cert: str = ""
    tls_key: str = ""
    # CA con la que se verifican los certificados de ControlNodes y otros
    # DataNodes (vacío = verificación por defecto del sistema).
    ca_cert: str = ""

    @property
    def controlnode_url_list(self) -> list[str]:
        urls = [u.strip().rstrip("/") for u in self.controlnode_urls.split(",") if u.strip()]
        return urls or [self.controlnode_url.rstrip("/")]

    @property
    def scheme(self) -> str:
        return "https" if self.tls_cert else "http"

    @property
    def internal_host(self) -> str:
        return self.datanode_internal_host or self.datanode_advertise_host

    @property
    def internal_port(self) -> int:
        return self.datanode_internal_port or self.datanode_advertise_port

    @property
    def verify(self):
        return self.ca_cert or True


settings = Settings()
