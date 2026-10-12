"""
Grupos de usuarios (Hito 3, control de acceso por grupo).

Cualquier usuario puede crear un grupo: queda como dueño y primer miembro, y
es el único que puede añadir o quitar miembros o borrarlo. Los grupos se
usan con `chgrp` (grupo dueño de un archivo/directorio, con los bits de
grupo del modo) y en las entradas de ACL (ver app/acl.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acl import is_superuser
from app.database import get_db
from app.deps import get_current_user
from app.models import AclEntry, Directory, File, Group, GroupMember, User
from app.schemas import GroupCreateRequest, GroupMemberRequest, GroupResponse

router = APIRouter()


def _error(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


def _response(group: Group) -> GroupResponse:
    return GroupResponse(
        name=group.name,
        owner=group.owner.username,
        members=sorted(member.user.username for member in group.members),
        created_at=group.created_at,
    )


def _get_group(db: Session, name: str) -> Group:
    group = db.scalar(select(Group).where(Group.name == name))
    if group is None:
        raise _error(404, "not_found", f"No existe el grupo {name!r}")
    return group


def _require_group_owner(user: User, group: Group) -> None:
    if group.owner_id != user.id and not is_superuser(user):
        raise _error(403, "permission_denied", f"Solo el dueño puede modificar el grupo {group.name!r}")


@router.post("", response_model=GroupResponse, status_code=201)
def create_group(payload: GroupCreateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if db.scalar(select(Group).where(Group.name == payload.name)) is not None:
        raise _error(409, "already_exists", f"Ya existe el grupo {payload.name!r}")
    group = Group(name=payload.name, owner_id=user.id)
    db.add(group)
    db.flush()
    db.add(GroupMember(group_id=group.id, user_id=user.id))
    db.commit()
    db.refresh(group)
    return _response(group)


@router.get("", response_model=list[GroupResponse])
def list_my_groups(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Grupos de los que el usuario es miembro."""
    groups = db.scalars(
        select(Group).join(GroupMember).where(GroupMember.user_id == user.id).order_by(Group.name)
    )
    return [_response(group) for group in groups]


@router.get("/{name}", response_model=GroupResponse)
def get_group(name: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _response(_get_group(db, name))


@router.post("/{name}/members", response_model=GroupResponse)
def add_member(
    name: str, payload: GroupMemberRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    group = _get_group(db, name)
    _require_group_owner(user, group)
    member = db.scalar(select(User).where(User.username == payload.username))
    if member is None:
        raise _error(404, "not_found", f"No existe el usuario {payload.username!r}")
    if not any(m.user_id == member.id for m in group.members):
        db.add(GroupMember(group_id=group.id, user_id=member.id))
        db.commit()
        db.refresh(group)
    return _response(group)


@router.delete("/{name}/members/{username}", response_model=GroupResponse)
def remove_member(name: str, username: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    group = _get_group(db, name)
    _require_group_owner(user, group)
    for member in list(group.members):
        if member.user.username == username:
            if member.user_id == group.owner_id:
                raise _error(400, "owner_member", "El dueño no puede salir de su propio grupo")
            db.delete(member)
    db.commit()
    db.refresh(group)
    return _response(group)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_group(name: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    group = _get_group(db, name)
    _require_group_owner(user, group)
    # Las entradas que lo usaban pierden el grupo y sus ACL de grupo.
    for model in (Directory, File):
        for row in db.scalars(select(model).where(model.group_id == group.id)):
            row.group_id = None
    for entry in db.scalars(select(AclEntry).where(AclEntry.group_id == group.id)):
        db.delete(entry)
    db.delete(group)
    db.commit()
