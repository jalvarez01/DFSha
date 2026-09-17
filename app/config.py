"""
Configuración global de la aplicación.

Centraliza los parámetros que dependen del entorno (ruta de la base de
datos, nombre del servicio, etc.) para que no queden "hardcodeados" en
otros módulos. Cualquiera de los tres módulos de mis compañeros puede
importar `settings` en lugar de leer variables de entorno por su cuenta.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Nombre del servicio, usado en /health y en logs.
    app_name: str = "DFSha"

    # URL de conexión de SQLAlchemy. Por defecto un archivo SQLite local.
    # En Docker se puede sobreescribir con la variable de entorno DATABASE_URL
    # para apuntar a un volumen persistente, p. ej. sqlite:////data/dfsha.db
    database_url: str = "sqlite:///./dfsha.db"

    # Tamaño máximo de un nombre de archivo/directorio (un solo segmento
    # de ruta), para evitar nombres absurdamente largos.
    max_name_length: int = 255

    # Firma de los JWT que emite POST /auth/login. El valor por defecto es
    # SOLO para desarrollo: en cualquier despliegue real hay que fijar la
    # variable de entorno DFSHA_JWT_SECRET.
    jwt_secret: str = "dev-secret-cambiar-en-produccion"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12

    # --- Hito 2: DFS distribuido por bloques (ControlNode) ---
    # Tamaño de bloque por defecto para particionar archivos. El cliente
    # puede pedir otro en /allocate, pero si no lo especifica se usa este.
    # 8 MiB es el mismo orden de magnitud que HDFS (64/128 MiB) ajustado a
    # un clúster de laboratorio.
    block_size_bytes: int = 8 * 1024 * 1024

    # Ventana de gracia para considerar "vivo" a un DataNode: si su último
    # heartbeat es más antiguo que esto, se excluye del reparto de bloques.
    # Debe ser holgadamente mayor que el intervalo con que los DataNodes
    # mandan heartbeat (p. ej. heartbeat cada 10 s, TTL de 30 s).
    heartbeat_ttl_seconds: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DFSHA_")


# Instancia única (singleton) que se importa desde el resto de la app.
settings = Settings()
