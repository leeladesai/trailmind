import secrets
from datetime import datetime, timedelta, timezone

from collections.abc import Callable

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AdminSession, User


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SESSION_LIFETIME = timedelta(hours=12)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_session_token(session: Session, user: User, settings: Settings) -> str:
    """Issues a JWT and persists a matching `AdminSession` row keyed by a random
    `jti` claim — the JWT's own signature/expiry can't be invalidated early, so
    revocation (logout, a detected leak, a future "sign out everywhere") works by
    checking this row instead (see get_current_user)."""
    jti = secrets.token_hex(16)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
    session.add(AdminSession(user_id=user.id, jti=jti, expires_at=expires_at))
    session.commit()
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "tenant_id": user.tenant_id,
        "jti": jti,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def revoke_session(session: Session, jti: str) -> None:
    """Immediate, explicit revocation — used by logout. A revoked session's JWT
    still verifies (signature/expiry are unaffected), but get_current_user rejects
    it the moment this row's revoked_at is set, regardless of how much of its
    12-hour lifetime remains."""
    admin_session = session.scalar(select(AdminSession).where(AdminSession.jti == jti))
    if admin_session is not None and admin_session.revoked_at is None:
        admin_session.revoked_at = datetime.now(timezone.utc)
        session.commit()


def revoke_all_sessions_for_user(session: Session, user_id: int) -> None:
    """Extension point for a future "sign out everywhere" / forced-logout admin
    action (e.g. on suspected compromise) — not yet wired to any endpoint."""
    now = datetime.now(timezone.utc)
    sessions = session.scalars(
        select(AdminSession).where(
            AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None)
        )
    ).all()
    for admin_session in sessions:
        admin_session.revoked_at = now
    session.commit()


def _extract_token(request: Request, settings: Settings) -> str | None:
    # The React admin frontend runs on its own origin and authenticates with a
    # bearer token (Authorization header) rather than a cookie — no CORS
    # credentials/SameSite coordination needed. The cookie is still accepted as a
    # fallback for anything still relying on it during the frontend migration.
    auth_header = request.headers.get("Authorization", "")
    return (
        auth_header.removeprefix("Bearer ").strip()
        if auth_header.startswith("Bearer ")
        else None
    ) or request.cookies.get(settings.session_cookie_name)


def revoke_current_session(
    request: Request, session: Session, settings: Settings
) -> None:
    """Best-effort logout-time revocation: decodes whatever token the request
    carries (cookie or bearer) and revokes its session row if one exists. Silently
    no-ops on a missing/invalid/already-expired token — logging out is never an
    error, even for a caller with no valid session left to revoke."""
    token = _extract_token(request, settings)
    if not token:
        return
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        jti = payload.get("jti")
    except JWTError:
        return
    if jti:
        revoke_session(session, jti)


def get_current_user(request: Request, session: Session, settings: Settings) -> User:
    token = _extract_token(request, settings)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        user_id = int(payload["sub"])
        jti = payload.get("jti")
    except (JWTError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        ) from None
    if jti is not None:
        # Older tokens issued before this field existed (or any minted with no
        # session row, e.g. directly in a test) have no jti to check — only a
        # token that names a *revoked* session is rejected here.
        admin_session = session.scalar(
            select(AdminSession).where(AdminSession.jti == jti)
        )
        if admin_session is not None and admin_session.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been signed out",
            )
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return user


def make_role_dependency(
    session_factory: Callable[[], Session],
    settings: Settings,
    required_role: str | tuple[str, ...] | None = None,
) -> Callable[[Request], User]:
    allowed_roles = (
        (required_role,) if isinstance(required_role, str) else required_role
    )

    def dependency(request: Request) -> User:
        session = session_factory()
        try:
            user = get_current_user(request, session, settings)
            if allowed_roles and user.role not in allowed_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin access required",
                )
            return user
        finally:
            session.close()

    return dependency
