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

    # --- Hito 3: replicación y consistencia ---
    # Número de DataNodes que guardan cada bloque. Si hay menos nodos vivos,
    # se usan todos los que haya y el re-replicador completa el factor
    # cuando aparezcan más.
    replication_factor: int = 3
    # Réplicas confirmadas mínimas para que `confirm` acepte un bloque (las
    # que falten las completa el re-replicador en segundo plano, como HDFS).
    min_replicas: int = 1
    # Cada cuánto el líder revisa bloques con menos réplicas de las debidas.
    replication_check_interval: float = 5.0
    # Una orden de copia que no se completa en este tiempo se reprograma.
    replication_task_timeout: float = 120.0
    # Gracia antes de ordenar borrar un bloque que salió del namespace: un
    # lector que pidió el plan justo antes todavía puede bajarlo.
    deletion_grace_seconds: float = 60.0
    # Locks de archivo (RF3 lock()) y leases implícitos de escritura.
    lease_ttl_seconds: int = 120
    max_lock_ttl_seconds: int = 3600
    # Vida de los tokens de bloque que firma el ControlNode para el cliente.
    block_token_ttl_seconds: int = 3600

    # --- Hito 3: seguridad ---
    # Secreto del clúster: firma los tokens entre nodos (DataNode <->
    # ControlNode, ControlNode <-> ControlNode) y los tokens de bloque que
    # el DataNode exige. Debe ser el mismo en todos los nodos y distinto del
    # jwt_secret de los usuarios.
    cluster_secret: str = "dev-cluster-secret-cambiar-en-produccion"
    # CA con la que se verifican los certificados TLS de los otros nodos
    # (vacío = verificación por defecto del sistema).
    ca_cert: str = ""
    # Permisos por defecto de lo que crea un usuario (privado: "cada cliente
    # gestiona solo sus propios archivos").
    default_dir_mode: int = 0o700
    default_file_mode: int = 0o600
    # Usuarios que se saltan el control de acceso (administración).
    superusers: str = ""
    # Autenticación heredada por header X-Username (sin contraseña). Solo
    # para tests y desarrollo: en el Hito 3 permitiría suplantar usuarios.
    allow_insecure_dev_auth: bool = False
    # Bloqueo temporal tras logins fallidos seguidos (fuerza bruta).
    max_login_failures: int = 5
    login_lockout_seconds: int = 300

    # --- Hito 3: alta disponibilidad del ControlNode ---
    # Identidad de este ControlNode dentro del grupo de HA.
    controlnode_id: str = "cn1"
    # URL con la que los clientes alcanzan a este ControlNode (informativa).
    public_url: str = ""
    # URLs (internas) de los otros ControlNodes, separadas por comas. Vacío
    # = ControlNode único, sin HA.
    ha_peers: str = ""
    # Rol con el que arranca: "primary" intenta ser líder; "standby" sigue
    # al líder y solo toma el control si este cae.
    ha_initial_role: str = "primary"
    # Sin contacto con el líder durante este tiempo, el respaldo puede
    # promoverse (si los DataNodes confirman que el líder no responde).
    ha_failover_seconds: float = 15.0
    # El líder espera hasta este tiempo a que el respaldo confirme cada
    # escritura antes de responder (replicación semi-síncrona).
    ha_sync_timeout: float = 2.0
    # Espera máxima del long-poll con el que el respaldo pide el log.
    ha_poll_wait: float = 1.0
    # Entradas del log de cambios que se conservan.
    ha_log_retention: int = 100_000

    @property
    def superuser_names(self) -> set[str]:
        return {name.strip() for name in self.superusers.split(",") if name.strip()}

    @property
    def ha_peer_urls(self) -> list[str]:
        return [url.strip().rstrip("/") for url in self.ha_peers.split(",") if url.strip()]

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DFSHA_", extra="ignore")


# Instancia única (singleton) que se importa desde el resto de la app.
settings = Settings()
