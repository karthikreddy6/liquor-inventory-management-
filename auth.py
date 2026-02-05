from datetime import datetime, timedelta, timezone
from flask import request, Response
import jwt
from config import (
    ADMIN_USER,
    ADMIN_PASS,
    JWT_SECRET,
    JWT_ALGORITHM,
    JWT_EXPIRES_MINUTES,
    OWNER_USER,
    OWNER_PASS,
    SUPERVISOR_USER,
    SUPERVISOR_PASS,
)

def admin_required(fn):
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response(
                "Unauthorized",
                401,
                {"WWW-Authenticate": 'Basic realm="Admin"'}
            )
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper

def _get_users():
    return {
        OWNER_USER: {"password": OWNER_PASS, "role": "owner"},
        SUPERVISOR_USER: {"password": SUPERVISOR_PASS, "role": "supervisor"},
    }

def authenticate_user(username: str, password: str):
    users = _get_users()
    user = users.get(username)
    if not user:
        return None
    if user["password"] != password:
        return None
    return {"username": username, "role": user["role"]}

def create_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRES_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def jwt_required(roles=None):
    roles = roles or []
    def decorator(fn):
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return {"error": "Missing or invalid Authorization header"}, 401
            token = auth.split(" ", 1)[1].strip()
            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            except Exception:
                return {"error": "Invalid or expired token"}, 401
            role = payload.get("role")
            if roles and role not in roles:
                return {"error": "Forbidden"}, 403
            request.user = {
                "username": payload.get("sub"),
                "role": role
            }
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator
