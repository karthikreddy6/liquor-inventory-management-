from datetime import datetime, timedelta, timezone
from flask import request, Response
import jwt
import base64
from functools import wraps
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

def get_auth_mode():
    """Helper to detect auth type from header"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "): return "basic"
    if auth_header.startswith("Bearer "): return "jwt"
    return None

def auth_required(roles=None):
    """
    Unified Decorator:
    1. Checks for Basic Auth (Admin)
    2. Fallback to JWT (Owner/Supervisor)
    """
    roles = roles or ["owner", "supervisor"]
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            
            # 1. Try Basic Auth (Admin)
            if auth_header.startswith("Basic "):
                try:
                    encoded = auth_header.split(" ", 1)[1]
                    decoded = base64.b64decode(encoded).decode("utf-8")
                    u, p = decoded.split(":", 1)
                    if u == ADMIN_USER and p == ADMIN_PASS:
                        request.user = {"username": u, "role": "admin"}
                        return fn(*args, **kwargs)
                except: pass
                return Response("Invalid Admin Credentials", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})

            # 2. Try JWT (Staff)
            if auth_header.startswith("Bearer "):
                token = auth_header.split(" ", 1)[1].strip()
                try:
                    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                    role = payload.get("role")
                    if roles and role not in roles and role != "admin":
                        return {"error": "Forbidden"}, 403
                    request.user = {"username": payload.get("sub"), "role": role}
                    return fn(*args, **kwargs)
                except:
                    return {"error": "Invalid or expired token"}, 401

            return {"error": "Missing or invalid Authorization header"}, 401
        return wrapper
    return decorator


def jwt_required(roles=None):
    """
    Backwards-compatible alias for routes still using jwt_required.
    Uses the unified auth_required under the hood.
    """
    return auth_required(roles=roles)
