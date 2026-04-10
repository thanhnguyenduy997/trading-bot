from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.user import User
from app.schemas.user import UserCreate
from app.schemas.user import UserUpdate


def create_user(db: Session, payload: UserCreate) -> User:
    user = User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
        full_name=payload.full_name,
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def list_users(db: Session) -> list[User]:
    return db.query(User).order_by(User.created_at.desc(), User.id.desc()).all()


def get_user(db: Session, user_id: int) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


def update_user(db: Session, user_id: int, payload: UserUpdate) -> User | None:
    user = get_user(db, user_id)
    if not user:
        return None
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def set_user_password(db: Session, user_id: int, password: str) -> User | None:
    user = get_user(db, user_id)
    if not user:
        return None
    user.hashed_password = get_password_hash(password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
