from datetime import datetime
from models import AuditLog, UserLogin


def log_action(db, user, action, entity_type="", entity_id="", details=""):
    if not user:
        return
    db.add(AuditLog(
        username=user.get("username"),
        role=user.get("role"),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else "",
        details=details or ""
    ))


def update_last_login(db, user):
    if not user:
        return
    username = user.get("username")
    role = user.get("role")
    if not username:
        return
    row = db.query(UserLogin).filter(UserLogin.username == username).first()
    if row:
        row.role = role
        row.last_login_at = datetime.utcnow()
    else:
        db.add(UserLogin(username=username, role=role, last_login_at=datetime.utcnow()))
