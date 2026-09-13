"""P0-4: the single writer for the append-only audit trail (`AuditLog`). Callers are
the HTTP boundary (app/main.py), right after a service call it triggered succeeds —
kept there rather than inside each service function so the service layer stays pure
and unaware of "who is doing this and why," while every admin-facing endpoint that
changes something security-relevant logs it the same way.
"""

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit_event(
    session: Session,
    *,
    action: str,
    actor_user_id: int | None,
    tenant_id: int | None = None,
    target_type: str | None = None,
    target_id: int | str | None = None,
    details: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details or {},
    )
    session.add(entry)
    session.commit()
    return entry
