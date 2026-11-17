"""Registration, login (JWT) and role-based access control."""
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from .db import IntegrityError, transaction
from .errors import ApiError
from .ledger import new_id, rupees

bp = Blueprint("auth", __name__)


def _secret():
    return os.environ.get("JWT_SECRET", "dev-secret-change-me")


def require_auth(*roles):
    """Decorator: valid Bearer JWT required; optionally restrict to the given roles."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise ApiError(401, "missing bearer token")
            try:
                claims = jwt.decode(header[7:], _secret(), algorithms=["HS256"])
            except jwt.PyJWTError:
                raise ApiError(401, "invalid or expired token")
            if roles and claims.get("role") not in roles:
                raise ApiError(403, "insufficient role")
            g.user = {"id": claims["sub"], "role": claims["role"]}
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@bp.post("/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password, role = data.get("password"), data.get("role", "CUSTOMER")
    if "@" not in email or not isinstance(password, str) or len(password) < 8 or role not in ("CUSTOMER", "MANAGER"):
        raise ApiError(400, "valid email, password (8+ chars) and role CUSTOMER|MANAGER required")
    user_id = new_id()
    try:
        with transaction() as tx:
            tx.run("INSERT INTO users (id, email, password_hash, role) VALUES (%s, %s, %s, %s)",
                   user_id, email, generate_password_hash(password), role)
            if role == "CUSTOMER":  # demo opening balance so transfers can be tried immediately
                tx.run("INSERT INTO accounts (id, user_id, balance_paise) VALUES (%s, %s, %s)",
                       new_id(), user_id, int(os.environ.get("OPENING_BALANCE_PAISE", 10_000_000)))
    except IntegrityError:
        raise ApiError(409, "email already registered")
    return jsonify(id=user_id, email=email, role=role), 201


@bp.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    with transaction() as tx:
        user = tx.run("SELECT * FROM users WHERE email = %s", str(data.get("email") or "").strip().lower()).one()
    if not user or not check_password_hash(user["password_hash"], str(data.get("password") or "")):
        raise ApiError(401, "invalid credentials")
    token = jwt.encode({"sub": user["id"], "role": user["role"],
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, _secret(), algorithm="HS256")
    return jsonify(token=token, role=user["role"])


@bp.get("/accounts/me")
@require_auth("CUSTOMER")
def my_account():
    with transaction() as tx:
        acc = tx.run("SELECT id, balance_paise FROM accounts WHERE user_id = %s", g.user["id"]).one()
    return jsonify(account_id=acc["id"], balance=rupees(acc["balance_paise"]))
