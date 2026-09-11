"""Tests for local auth + OIDC SSO (port of Dockhand's auth).

Runs without a running Flask/SQLAlchemy stack: it loads ``app/auth.py`` and
``app/oidc.py`` with a stubbed ``app.db``/``app.models`` and a fake ``requests``
module, so it can verify hashing, PKCE, JWT decode, claim mapping, the
authorization-URL builder and the callback flow deterministically.

Run with:  python -m pytest tests/test_auth_oidc.py -q
"""

import base64
import hashlib
import importlib.util
import json
import os
import sys
import types

import pytest
import werkzeug.security as _ws

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")


@pytest.fixture(scope="module")
def load():
    """Load auth + oidc under a fake ``app`` package and return them."""
    pkg = types.ModuleType("app")
    pkg.__path__ = [APP_DIR]
    sys.modules["app"] = pkg

    db = types.ModuleType("app.db")
    sys.modules["app.db"] = db
    pkg.db = db
    db.session = None
    db.Model = type("Model", (), {})

    models = types.ModuleType("app.models")
    sys.modules["app.models"] = models
    pkg.models = models
    models.User = type("User", (db.Model,), {})
    models.Session = type("Session", (db.Model,), {})
    models.Setting = type("Setting", (db.Model,), {})
    models.OidcConfig = type("OidcConfig", (db.Model,), {})

    auth = _load_module("app.auth", os.path.join(APP_DIR, "auth.py"), "app.auth")
    oidc = _load_module("app.oidc", os.path.join(APP_DIR, "oidc.py"), "app.oidc")
    return auth, oidc, models


def _load_module(fqname, path, alias):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Local auth
# --------------------------------------------------------------------------- #

def test_password_roundtrip(load):
    auth, _, _ = load
    h = auth.hash_password("correct horse")
    assert h != "correct horse"
    assert auth.verify_password("correct horse", h) is True
    assert auth.verify_password("wrong", h) is False


def test_empty_hash_rejected(load):
    auth, _, _ = load
    assert auth.verify_password("anything", "") is False
    assert auth.verify_password("anything", None) is False


def test_dummy_hash_cached_and_different_from_real(load):
    auth, _, _ = load
    assert auth.dummy_hash() == auth.dummy_hash()
    real = auth.hash_password("real-password")
    assert auth.dummy_hash() != real


# --------------------------------------------------------------------------- #
# OIDC primitives
# --------------------------------------------------------------------------- #

def test_pkce_s256(load):
    _, oidc, _ = load
    verifier, challenge = oidc.generate_pkce()
    assert len(verifier) >= 43
    assert len(challenge) >= 43
    recomputed = oidc._b64url(hashlib.sha256(verifier.encode()).digest())
    assert recomputed == challenge


def test_jwt_payload_decode(load):
    _, oidc, _ = load
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    token = f"h.{b64(json.dumps({'sub': 'u1', 'email': 'a@b.c'}).encode())}.sig"
    claims = oidc._decode_jwt_payload(token)
    assert claims["sub"] == "u1"
    assert claims["email"] == "a@b.c"


def test_claim_matches(load):
    _, oidc, _ = load
    assert oidc._claim_matches({"groups": ["admins", "ops"]}, "groups", "admins,staff") is True
    assert oidc._claim_matches({"groups": "users"}, "groups", "admins") is False
    assert oidc._claim_matches({}, "groups", "admins") is False
    assert oidc._claim_matches({"groups": ["admins"]}, "", "admins") is False


# --------------------------------------------------------------------------- #
# Authorization URL (mocked discovery)
# --------------------------------------------------------------------------- #

def test_build_authorization_url(load):
    _, oidc, models = load

    class Resp:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {
                "issuer": "https://idp",
                "authorization_endpoint": "https://idp/oauth2/authorize",
                "token_endpoint": "https://idp/oauth2/token",
                "userinfo_endpoint": "https://idp/oauth2/userinfo",
            }

    oidc.requests = types.SimpleNamespace(get=lambda url, **kw: Resp())

    cfg = models.OidcConfig()
    cfg.id = 9
    cfg.issuer_url = "https://idp"
    cfg.client_id = "client-123"
    cfg.client_secret = "secret"
    cfg.redirect_uri = "https://app/callback"
    cfg.scopes = "openid profile email"
    cfg.username_claim = "preferred_username"
    cfg.email_claim = "email"
    cfg.display_name_claim = "name"
    cfg.admin_claim = "groups"
    cfg.admin_value = "admins"

    result = oidc.build_authorization_url(cfg, "/dashboard")
    assert "error" not in result, result
    assert result["url"].startswith("https://idp/oauth2/authorize?")
    for param in ("response_type=code", "client_id=client-123",
                  "code_challenge_method=S256", "state=", "nonce="):
        assert param in result["url"], param


def test_build_authorization_url_bad_issuer(load):
    _, oidc, models = load
    cfg = models.OidcConfig()
    cfg.id = 1
    cfg.issuer_url = "not-a-url"
    result = oidc.build_authorization_url(cfg, "/")
    assert result.get("error")


# --------------------------------------------------------------------------- #
# Callback flow (mocked discovery + token/userinfo + user create)
# --------------------------------------------------------------------------- #

def test_oidc_callback_creates_user(load):
    _, oidc, models = load
    created = {}

    class Resp:
        def __init__(self, data=None, ok=True):
            self.data = data or {}
            self.ok = ok

        def json(self):
            return self.data

    _token_payload = {}

    def fake_get(url, **kw):
        if "userinfo" in url:
            return Resp(data=_token_payload)
        return Resp(data={
            "issuer": "https://idp",
            "authorization_endpoint": "https://idp/auth",
            "token_endpoint": "https://idp/token",
            "userinfo_endpoint": "https://idp/userinfo",
        })

    def fake_post(url, **kw):
        return Resp(data={
            "access_token": "AT",
            "id_token": "h.p.s",
            "token_type": "Bearer",
        })

    oidc.requests = types.SimpleNamespace(get=fake_get, post=fake_post)

    # Build a real state first.
    cfg = models.OidcConfig()
    cfg.id = 1
    cfg.name = "Keycloak"
    cfg.enabled = True
    cfg.issuer_url = "https://idp"
    cfg.client_id = "cid"
    cfg.client_secret = "sec"
    cfg.redirect_uri = "https://app/cb"
    cfg.scopes = "openid"
    cfg.username_claim = "preferred_username"
    cfg.email_claim = "email"
    cfg.display_name_claim = "name"
    cfg.admin_claim = "groups"
    cfg.admin_value = "admins"

    conf_map = {1: cfg}
    oidc.db = types.SimpleNamespace(
        session=types.SimpleNamespace(
            get=lambda cls, cid: conf_map.get(cid),
            add=lambda u: created.__setitem__("u", u),
            commit=lambda: None,
        )
    )

    class FakeUser:
        query = type("Q", (), {
            "filter_by": staticmethod(lambda **k: type("R", (), {
                "first": staticmethod(lambda: None)
            })())
        })()

        def __init__(self, **kw):
            self.__dict__.update(kw)

    oidc.User = FakeUser

    urlres = oidc.build_authorization_url(cfg, "/dash")
    state = urlres["url"].split("state=")[1].split("&")[0]
    nonce = oidc.STATE_STORE[state]["nonce"]

    _token_payload = {
        "sub": "u-123",
        "preferred_username": "alice",
        "email": "alice@corp.com",
        "name": "Alice",
        "groups": ["admins"],
        "nonce": nonce,
    }

    result = oidc.handle_callback("the-code", state)
    assert result["success"], result
    user = created["u"]
    assert user.username == "alice"
    assert user.is_admin is True
    assert user.auth_provider == "oidc:Keycloak"
    assert user.email == "alice@corp.com"
    assert user.password_hash == ""


def test_oidc_callback_state_replay_rejected(load):
    _, oidc, models = load
    # A bogus state with no store entry must be rejected.
    result = oidc.handle_callback("code", "no-such-state")
    assert result["success"] is False
    assert "state" in result["error"].lower()