"""
Autenticación: login con usuario/contraseña que devuelve un JWT.

Es la única puerta de entrada para obtener un token; el resto de la API lo
consume a través de `get_current_user` (app/deps.py).

Sobre el registro de usuarios: el enunciado del Hito 1 pide "autenticación
básica (user/pass)" y no un flujo de alta de usuarios, así que no hay
endpoint de registro. En su lugar, el primer login de un username sin
contraseña fija la que se haya enviado (trust on first use) y los logins
siguientes la verifican. Eso cubre tanto a un usuario nuevo como a los que
quedaron creados por el header X-Username antes de que existiera el login.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.schemas import LoginRequest, TokenResponse
from app.security import create_access_token, hash_password, verify_password

router = APIRouter()

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "invalid_credentials", "message": "Usuario o contraseña incorrectos"},
    headers={"WWW-Authenticate": "Bearer"},
)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Verifica las credenciales y emite un JWT.

    Responde siempre 401 con el mismo mensaje si algo falla, para no
    filtrar qué usernames existen.
    """
    username = payload.username.strip()
    if not username:
        raise _INVALID_CREDENTIALS

    user = db.scalar(select(User).where(User.username == username))

    if user is None:
        # Usuario nuevo: se crea con la contraseña enviada.
        user = User(username=username, password_hash=hash_password(payload.password))
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.password_hash is None:
        # Usuario preexistente sin contraseña (creado vía X-Username, o el
        # 'system' del arranque): este login se la fija.
        user.password_hash = hash_password(payload.password)
        db.commit()
        db.refresh(user)
    elif not verify_password(payload.password, user.password_hash):
        raise _INVALID_CREDENTIALS

    token, expires_in = create_access_token(username=user.username, user_id=user.id)
    return TokenResponse(access_token=token, expires_in=expires_in, username=user.username)
