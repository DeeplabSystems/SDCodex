"""OIDC SSO (OpenID Connect), porting Dockhand's ``auth.ts`` OIDC section.

Flow (Authorization Code + PKCE):

1. ``build_authorization_url(config)`` — fetch the provider discovery document
   (cached 1h), mint ``state`` + ``nonce`` + PKCE challenge, store the verifier
   server-side (10 min TTL), and return the IdP authorization URL.
2. User signs in at the IdP and is redirected back to ``/auth/oidc/callback``.
3. ``handle_callback(code, state)`` — validate+consume state, exchange the code
   at the token endpoint with ``code_verifier``, decode the ID-token JWT payload,
   enrich with the userinfo endpoint, map claims to a local user (get-or-create,
   linked to the provider), apply the admin claim, and hand back the user so the
   route can create a session.

Pure stdlib + ``requests``; unit-testable with a mocked HTTP layer.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time

import requests

from .models import OidcConfig, User
from . import db

logger = logging.getLogger(__name__)

DISCOVERY_TTL = 3600  # seconds
CALLBACK_TTL = 600  # state expiry (10 min)
STATE_STORE = {}  # state -> {config_id, code_verifier, nonce, redirect_url, expires_at}

_discovery_cache = {}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _well_known_urls(issuer: str) -> list:
    """Return candidate discovery URLs (canonical, then trailing-slash variant
    some providers require)."""
    issuer = (issuer or "").strip().rstrip("/")
    if not issuer:
        return []
    base = f"{issuer}/.well-known/openid-configuration"
    return [base, f"{issuer}/.well-known/openid-configuration/", f"{issuer}/.well-known/openid-configuration"]


def fetch_discovery(issuer: str):
    """Return the discovery document (dict), with a 1h in-process cache."""
    key = (issuer or "").strip()
    if not key:
        raise ValueError("issuer URL is required")
    cached = _discovery_cache.get(key)
    if cached and cached["expires_at"] > time.time():
        return cached["doc"]
    if not key.startswith("https://") and not key.startswith("http://"):
        raise ValueError("issuer URL must be http(s)")
    last_err = None
    for url in _well_known_urls(issuer):
        try:
            resp = requests.get(url, timeout=10)
            if resp.ok:
                doc = resp.json()
                _discovery_cache[key] = {"doc": doc, "expires_at": time.time() + DISCOVERY_TTL}
                return doc
            last_err = f"{resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
    raise ValueError(f"Failed to fetch OIDC discovery document for {issuer}: {last_err}")


# --------------------------------------------------------------------------- #
# PKCE / helpers
# --------------------------------------------------------------------------- #

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce():
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def test_oidc_config(config: OidcConfig) -> dict:
    try:
        doc = fetch_discovery(config.issuer_url)
        return {
            "success": True,
            "issuer": doc.get("issuer"),
            "endpoints": {
                "authorization": doc.get("authorization_endpoint"),
                "token": doc.get("token_endpoint"),
                "userinfo": doc.get("userinfo_endpoint"),
            },
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# State store
# --------------------------------------------------------------------------- #

def _prune_state():
    now = time.time()
    for k in [k for k, v in STATE_STORE.items() if v["expires_at"] < now]:
        STATE_STORE.pop(k, None)


def build_authorization_url(config: OidcConfig, redirect_url: str = "/"):
    """Return ``{"url": ..., "error"?: str}``. Stores PKCE verifier + nonce."""
    try:
        discovery = fetch_discovery(config.issuer_url)
        state = _b64url(secrets.token_bytes(32))
        nonce = _b64url(secrets.token_bytes(16))
        verifier, challenge = generate_pkce()
        _prune_state()
        STATE_STORE[state] = {
            "config_id": config.id,
            "code_verifier": verifier,
            "nonce": nonce,
            "redirect_url": redirect_url,
            "expires_at": time.time() + CALLBACK_TTL,
        }
        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scopes or "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        query = "&".join(f"{k}={_urlencode(v)}" for k, v in params.items())
        auth_endpoint = discovery.get("authorization_endpoint")
        if not auth_endpoint:
            return {"error": "Provider discovery has no authorization_endpoint"}
        return {"url": f"{auth_endpoint}?{query}"}
    except Exception as exc:
        logger.exception("Failed to build OIDC authorization URL")
        return {"error": str(exc)}


def _urlencode(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")


# --------------------------------------------------------------------------- #
# Callback
# --------------------------------------------------------------------------- #

def handle_callback(code: str, state: str):
    """Exchange code for tokens, resolve a local user, return a result dict."""
    _prune_state()
    state_data = STATE_STORE.pop(state, None)
    if not state_data:
        return {"success": False, "error": "Invalid or expired state"}
    if state_data["expires_at"] < time.time():
        return {"success": False, "error": "SSO session expired"}

    config = db.session.get(OidcConfig, state_data["config_id"])
    if not config or not config.enabled:
        return {"success": False, "error": "OIDC configuration not found or disabled"}

    try:
        discovery = fetch_discovery(config.issuer_url)

        token_resp = requests.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": state_data["code_verifier"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not token_resp.ok:
            return {"success": False, "error": f"Token exchange failed: {token_resp.text[:200]}"}
        tokens = token_resp.json()

        # Decode ID token payload (base64url JWT). Basic validation only — matches
        # Dockhand's comment that production should use a JWT library.
        claims = {}
        if tokens.get("id_token"):
            claims = _decode_jwt_payload(tokens["id_token"])

        # Enrich with userinfo when available.
        userinfo_ep = discovery.get("userinfo_endpoint")
        if userinfo_ep and tokens.get("access_token"):
            try:
                ui = requests.get(
                    userinfo_ep,
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                    timeout=10,
                )
                if ui.ok:
                    claims = {**claims, **ui.json()}
            except requests.RequestException:
                pass

        # Nonce check.
        if claims.get("nonce") and claims.get("nonce") != state_data["nonce"]:
            return {"success": False, "error": "Invalid nonce"}

        username = claims.get(config.username_claim) or claims.get("preferred_username") or claims.get("sub")
        email = claims.get(config.email_claim) or claims.get("email")
        display_name = claims.get(config.display_name_claim) or claims.get("name")
        if not username:
            return {"success": False, "error": "Username claim not found in token"}

        should_be_admin = _claim_matches(claims, config.admin_claim, config.admin_value)

        provider = f"oidc:{config.name}"
        user = User.query.filter_by(username=username).first()
        if not user:
            user = User(
                username=username,
                email=(email or None),
                display_name=(display_name or None),
                password_hash="",
                auth_provider=provider,
                is_active=True,
                is_admin=bool(should_be_admin),
            )
            db.session.add(user)
        else:
            if email:
                user.email = email
            if display_name:
                user.display_name = display_name
            user.auth_provider = provider
            if should_be_admin:
                user.is_admin = True  # never strip admin (prevents lockouts)
        db.session.commit()

        if not user.is_active:
            return {"success": False, "error": "Account is disabled"}

        return {"success": True, "user": user, "redirect_url": state_data["redirect_url"]}
    except Exception as exc:
        logger.exception("[OIDC] callback error")
        return {"success": False, "error": str(exc)}


def _claim_matches(claims: dict, claim_name: str, expected: str) -> bool:
    if not claim_name or not expected:
        return False
    value = claims.get(claim_name)
    expected_values = [v.strip() for v in expected.split(",")]
    if isinstance(value, list):
        return any(str(v) in expected_values for v in value)
    return str(value) in expected_values if value is not None else False


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #

def enabled_configs():
    return OidcConfig.query.filter_by(enabled=True).order_by(OidcConfig.name).all()


def all_configs():
    return OidcConfig.query.order_by(OidcConfig.name).all()