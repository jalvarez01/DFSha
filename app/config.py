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

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DFSHA_")


# Instancia única (singleton) que se importa desde el resto de la app.
settings = Settings()
