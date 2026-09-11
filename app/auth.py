"""Local authentication + sessions + a small authorize helper.

Ports the essential pieces of Dockhand's ``auth.ts`` / ``authorize.ts`` to this
Flask app:

* constant-time password hashing (werkzeug scrypt — ships with Flask, so no new
  dependency),
* server-side sessions backed by a random token in an httpOnly cookie
  (``sameSite=lax`` — required for OIDC cross-site redirects; ``Secure`` only
  when behind an HTTPS reverse proxy),
* ``authenticate_local`` (constant-time, safe against user-enumeration via a
  dummy hash),
* ``before_request`` wiring helpers so routes can require a valid session.

Everything here is pure logic over the SQLAlchemy models; it imports no Flask
blueprints, so it is unit-testable in isolation (the tests stub ``flask.request``
where needed).
"""

import logging
import os
import secrets
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash

from . import db
from .models import User, Session

logger = logging.getLogger(__name__)

# Settings keys stored in the ``Setting`` table (auth-relevant ones).
SETTING_AUTH_ENABLED = "auth_enabled"          # "1"/"0"
SETTING_SESSION_TIMEOUT = "auth_session_timeout_sec"  # default 86400

SESSION_COOKIE = "sdcodex_session"
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SDCODEX_SESSION_TIMEOUT", "86400"))

# Basic in-memory brute-force limiter, keyed "<ip>:<username>".
# (Mirrors Dockhand's rate-limiter; resets on restart — acceptable for local use.)
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300
_attempts = {}


# --------------------------------------------------------------------------- #
# Password hashing (constant-time via werkzeug)
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    return generate_password_hash(password or "")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return check_password_hash(password_hash, password or "")
    except Exception:  # malformed/legacy hash
        logger.warning("Password verification raised; treating as failure")
        return False


_dummy_hash = None


def dummy_hash() -> str:
    """Cache a hash of a random password so enumeration-resistant checks don't
    recompute scrypt on every miss (and, combined with a wrong account, spend the
    same cost on a valid-looking path)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(f"sdcodex-dummy-{secrets.token_hex(8)}")
    return _dummy_hash


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

def _now_ts() -> float:
    import time
    return time.time()


def rate_limited(key: str) -> bool:
    window_start = _now_ts() - _WINDOW_SECONDS
    attempts = [t for t in _attempts.get(key, []) if t > window_start]
    return len(attempts) >= _MAX_ATTEMPTS


def record_failed(key: str) -> None:
    key = (key or "").strip()
    if not key:
        return
    window_start = _now_ts() - _WINDOW_SECONDS
    _attempts.setdefault(key, []).append(_now_ts())
    _attempts[key] = [t for t in _attempts[key] if t > window_start]


def clear_failures(key: str) -> None:
    _attempts.pop((key or "").strip(), None)


# --------------------------------------------------------------------------- #
# Settings access
# --------------------------------------------------------------------------- #

def is_auth_enabled() -> bool:
    from .models import Setting
    row = db.session.get(Setting, SETTING_AUTH_ENABLED)
    return bool(row and row.value == "1")


def set_auth_enabled(flag: bool) -> None:
    from .models import Setting
    row = db.session.get(Setting, SETTING_AUTH_ENABLED)
    if row is None:
        row = Setting(key=SETTING_AUTH_ENABLED, value="1" if flag else "0")
        db.session.add(row)
    else:
        row.value = "1" if flag else "0"
    db.session.commit()


def session_timeout_seconds() -> int:
    from .models import Setting
    row = db.session.get(Setting, SETTING_SESSION_TIMEOUT)
    if not row or not row.value:
        return SESSION_TIMEOUT_SECONDS
    try:
        return max(60, int(row.value))
    except (TypeError, ValueError):
        return SESSION_TIMEOUT_SECONDS


def set_session_timeout_seconds(value: int) -> None:
    from .models import Setting
    row = db.session.get(Setting, SETTING_SESSION_TIMEOUT)
    if row is None:
        row = Setting(key=SETTING_SESSION_TIMEOUT, value=str(max(60, int(value))))
        db.session.add(row)
    else:
        row.value = str(max(60, int(value)))
    db.session.commit()


# --------------------------------------------------------------------------- #
# Session management
# --------------------------------------------------------------------------- #

def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def cookie_secure_ctx() -> bool:
    """Whether to set Secure on the session cookie.

    Respects an explicit env override, then trusts an HTTPS reverse proxy
    (x-forwarded-proto). Defaults to False so plain-HTTP homelab installs (the
    common case) don't silently drop the cookie and cause a login loop.
    """
    override = os.environ.get("SDCODEX_COOKIE_SECURE")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes")
    try:
        from flask import request
        return request.headers.get("X-Forwarded-Proto", "").lower() == "https"
    except Exception:
        return False


def create_session(user_id: int, provider: str = "local") -> str:
    """Create a session row and return its token (caller sets the cookie)."""
    token = _generate_token()
    timeout = session_timeout_seconds()
    expires_at = datetime.utcnow() + timedelta(seconds=timeout)
    sess = Session(id=token, user_id=user_id, provider=provider, expires_at=expires_at)
    db.session.add(sess)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    user = db.session.get(User, user_id)
    if user:
        user.last_login = datetime.utcnow()
        db.session.commit()
    return token


def set_session_cookie(response, token: str) -> None:
    max_age = session_timeout_seconds()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=cookie_secure_ctx(),
        samesite="Lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def get_session_token_from_request() -> str | None:
    try:
        from flask import request
        return request.cookies.get(SESSION_COOKIE)
    except Exception:
        return None


def validate_session(token: str | None) -> User | None:
    if not token:
        return None
    sess = db.session.get(Session, token)
    if not sess:
        return None
    if sess.expires_at < datetime.utcnow():
        db.session.delete(sess)
        db.session.commit()
        return None
    user = db.session.get(User, sess.user_id)
    if not user or not user.is_active:
        return None
    return user


def destroy_session(token: str | None) -> None:
    if not token:
        return
    sess = db.session.get(Session, token)
    if sess:
        db.session.delete(sess)
        db.session.commit()


def current_user() -> User | None:
    return validate_session(get_session_token_from_request())


# --------------------------------------------------------------------------- #
# Local authentication
# --------------------------------------------------------------------------- #

def authenticate_local(username: str, password: str):
    """Return a dict-shaped result: ``{success: bool, user?: User, error?: str}``.

    Uses the dummy-hash trick to keep timing constant regardless of whether the
    username exists (mirrors Dockhand's ``authenticateLocal``).
    """
    username = (username or "").strip()
    user = User.query.filter_by(username=username).first() if username else None

    if not user:
        verify_password(password, dummy_hash())
        return {"success": False, "error": "Invalid username or password"}

    if not user.is_active:
        verify_password(password, dummy_hash())
        return {"success": False, "error": "Invalid username or password"}

    if not verify_password(password or "", user.password_hash):
        return {"success": False, "error": "Invalid username or password"}

    # Accounts created via OIDC can't log in with a local password.
    if user.auth_provider.startswith("oidc:"):
        return {"success": False, "error": "This account signs in via SSO"}

    return {"success": True, "user": user}


# --------------------------------------------------------------------------- #
# Authorize helper
# --------------------------------------------------------------------------- #

def require_authenticated_request() -> User | None:
    """Enforce that the current request has a valid session, when auth is on.

    Returns the authenticated User, or None if auth is disabled. Raises… no —
    callers use this in a route guard and decide how to redirect.
    """
    if not is_auth_enabled():
        return None
    return current_user()