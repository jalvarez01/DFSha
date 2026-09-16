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
    # nuevo y los bloques que tenía asignados quedan "huérfanos").
    datanode_id: str = "dn-local"

    # Dónde vive el ControlNode (para register/heartbeat).
    controlnode_url: str = "http://localhost:8000"

    # host:puerto que el ControlNode debe anunciar a los CLIENTES para que
    # le hablen directo a este DataNode (PUT/GET/DELETE /blocks/...). En
    # docker-compose es el nombre del servicio (p. ej. "datanode-1") y el
    # mismo puerto en el que este proceso escucha (ver Dockerfile: uvicorn
    # se levanta con --port $DFSHA_DATANODE_ADVERTISE_PORT); en local,
    # localhost.
    datanode_advertise_host: str = "localhost"
    datanode_advertise_port: int = 9001

    # Cada cuánto se manda heartbeat al ControlNode.
    heartbeat_interval: float = 10.0

    # Dónde guarda los bloques en disco: <storage_dir>/<file_id>/<block_id>
    # ("archivo como directorio, bloque como archivo" — decisión de equipo).
    # Nombre de variable acordado en docker-compose.yml.
    datanode_storage_dir: Path = Path("/data/blocks")


settings = Settings()
