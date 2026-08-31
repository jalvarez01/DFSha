"""
Dependencias de FastAPI compartidas (además de `get_db`, que vive en
app/database.py por estar ligada al engine).

En el Hito 1 no hay autenticación real todavía. Para que mis compañeros
puedan implementar sus endpoints ya (necesitan un owner_id para crear
archivos/directorios) sin bloquearse esperando el sistema de login,
`get_current_user` resuelve el usuario a partir de un header simple
`X-Username`, creándolo si es la primera vez que se ve ese nombre.

Cuando se implemente autenticación de verdad, esta función es el único
lugar que hay que reemplazar: todos los endpoints que dependen de
`get_current_user` seguirán funcionando igual.
"""
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User


def get_current_user(
    x_username: str | None = Header(default=None, alias="X-Username"),
    db: Session = Depends(get_db),
) -> User:
    """Resuelve el usuario "actual" a partir del header `X-Username`.

    Placeholder deliberado para el Hito 1 (sin login real). Si el header
    no viene, responde 401 para dejar claro que todo endpoint protegido
    necesita identificarse de alguna forma, aunque sea así de simple.
    """
    if not x_username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthenticated", "message": "Falta el header X-Username"},
        )

    user = db.scalar(select(User).where(User.username == x_username))
    if user is None:
        user = User(username=x_username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
