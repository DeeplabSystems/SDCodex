"""Tests for the in-UI self-update (Docker Engine API + self-update orchestration).

These run without a Docker daemon: the Docker API client is exercised against an
in-memory Unix-socket fake, and the pure helper functions are unit-tested. Run
with:  python -m pytest tests/test_docker_selfupdate.py -q
"""

import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import types

import pytest


def _load(pkg_name, path, as_name):
    path = os.path.abspath(path)
    # Load docker_api as a plain module (it has no relative imports) and register
    # it under the fake package so docker_selfupdate's `from . import docker_api`
    # resolves.
    if as_name == "docker_api":
        spec = importlib.util.spec_from_file_location("docker_api", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["docker_api"] = mod
        sys.modules[f"{pkg_name}.docker_api"] = mod
        return mod
    # docker_selfupdate uses `from . import docker_api`; fake a package.
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    pkg.docker_api = sys.modules["docker_api"]
    sys.modules[pkg_name] = pkg
    sys.modules[f"{pkg_name}.docker_api"] = sys.modules["docker_api"]
    fq = f"{pkg_name}.docker_selfupdate"
    spec = importlib.util.spec_from_file_location(fq, path)
    dot = importlib.util.module_from_spec(spec)
    sys.modules[fq] = dot
    sys.modules[as_name] = dot
    spec.loader.exec_module(dot)
    return dot


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_API = _load("appx", os.path.join(ROOT, "app", "docker_api.py"), "docker_api")
SELFUPDATE = _load("appx", os.path.join(ROOT, "app", "docker_selfupdate.py"), "appx_docker_selfupdate")
SELFUPDATE.docker_api = DOCKER_API


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_split_image_ref():
    assert SELFUPDATE._split_image_ref("org/app:latest") == ("org/app", "latest")
    assert SELFUPDATE._split_image_ref("org/app") == ("org/app", "latest")
    assert SELFUPDATE._split_image_ref("org/app:2.1.0") == ("org/app", "2.1.0")


def test_build_network_env():
    info = {"NetworkSettings": {"Networks": {
        "web": {"IPAMConfig": {"IPv4Address": "10.0.0.5"}, "Aliases": ["alias1"]},
        "default": {},
    }}}
    envs = SELFUPDATE.build_network_env(info)
    assert envs[0] == "NETWORKS=web default"
    assert "NETWORK_OPTS_web=--ip 10.0.0.5 --alias alias1" in envs


def test_build_create_config():
    inspect = {
        "Config": {"Image": "old", "Cmd": ["x"], "Entrypoint": ["y"], "Hostname": "zz",
                   "Volumes": {"/data/db": {}, "/app": {}}, "Env": ["A=1"]},
        "HostConfig": {},
        "Mounts": [
            {"Type": "bind", "Source": "/home/me/db", "Destination": "/data/db", "RW": True},
            {"Type": "volume", "Name": "v1", "Destination": "/vol"},
        ],
    }
    cc = SELFUPDATE.build_create_config(inspect, "new:img")
    assert cc["Image"] == "new:img"
    for key in ("Entrypoint", "Cmd", "Hostname", "MacAddress", "NetworkingConfig"):
        assert key not in cc, key
    assert "/home/me/db:/data/db:rw" in cc["HostConfig"]["Binds"]
    # /data/db is bound -> must not remain in Volumes
    assert "/data/db" not in (cc.get("Volumes") or {})
    # /app has no bind -> stays as an anonymous volume
    assert "/app" in (cc.get("Volumes") or {})


def test_rebuild_binds_same_src_two_dests_no_duplicate_mount():
    # Regression: two binds sharing one host src must keep distinct container
    # dests. The old reversed dest:src order collapsed both onto the same
    # container path -> Docker 400 "Duplicate mount point".
    mounts = [
        {"Type": "bind", "Source": "/home/naked/ai/LLModels",
         "Destination": "/data/caption_models", "RW": True},
        {"Type": "bind", "Source": "/home/naked/ai/LLModels",
         "Destination": "/data/lmstudio_models", "RW": True},
    ]
    binds = SELFUPDATE._rebuild_binds(mounts)
    assert binds == [
        "/home/naked/ai/LLModels:/data/caption_models:rw",
        "/home/naked/ai/LLModels:/data/lmstudio_models:rw",
    ]
    dests = [b.split(":")[1] for b in binds]
    assert len(set(dests)) == len(dests)


def test_decode_container_logs():
    def frame(stream_type, payload):
        return struct.pack("B3xI", stream_type, len(payload)) + payload
    raw = frame(1, b"Stopping container\n") + frame(2, b"ERROR: nope\n")
    out = DOCKER_API.decode_container_logs(raw)
    assert "Stopping container" in out
    assert "ERROR: nope" in out
    # plain text fallback
    assert DOCKER_API.decode_container_logs(b"Starting\nDone\n") == "Starting\nDone"


# --------------------------------------------------------------------------- #
# Unix-socket HTTP client (in-memory fake daemon)
# --------------------------------------------------------------------------- #

class _FakeDaemon:
    """A tiny Docker-API-shaped HTTP server on a Unix socket."""

    def __init__(self):
        self.path = tempfile.mktemp(suffix=".sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        self.routes = {}
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def add(self, route, handler):
        self.routes[route] = handler

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(3)
                try:
                    data = conn.recv(65536).decode("utf-8", "replace")
                except OSError:
                    continue
                if not data:
                    continue
                line = data.split("\r\n")[0]
                method, path, _ = line.split(" ", 2)
                qs_path = path.split("?")[0]
                handler = self.routes.get(qs_path)
                status = 200
                body = b"{}"
                if handler:
                    try:
                        status, body = handler(method, path)
                    except Exception:
                        status, body = 500, b'{"error":"fake"}'
                elif qs_path == "/_ping":
                    status, body = 200, b"{}"
                else:
                    status, body = 404, b'{"message":"no such container"}'
                self._send(conn, status, body)

    @staticmethod
    def _send(conn, status, body):
        conn.sendall(
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )


@pytest.fixture
def fake_daemon():
    d = _FakeDaemon()
    old = os.environ.get("DOCKER_HOST")
    os.environ.pop("DOCKER_HOST", None)
    DOCKER_API.DEFAULT_SOCKET_PATH = d.path
    yield d
    d._stop.set()
    d.sock.close()
    try:
        os.unlink(d.path)
    except OSError:
        pass
    if old is not None:
        os.environ["DOCKER_HOST"] = old


def test_request_json(fake_daemon):
    fake_daemon.add("/containers/create", lambda m, p:
                    (201, json.dumps({"Id": "abcd"}).encode()))
    assert DOCKER_API.request("POST", "/containers/create?name=x", body={"Image": "i"}) \
        == {"Id": "abcd"}


def test_request_raw(fake_daemon):
    fake_daemon.add("/images/create", lambda m, p: (200, b'{"stream":"Pull..."}\n'))
    assert DOCKER_API.request_raw("POST", "/images/create") \
        == b'{"stream":"Pull..."}\n'


def test_request_404_raises(fake_daemon):
    with pytest.raises(DOCKER_API.DockerApiError):
        DOCKER_API.request("GET", "/containers/nope/json")


def test_is_docker_writable_from_mounts(fake_daemon):
    fake_daemon.add("/containers/abc/json", lambda m, p:
                    (200, json.dumps({"Mounts": [
                        {"Destination": DOCKER_API.DEFAULT_SOCKET_PATH, "RW": False}]}).encode()) if False
                    else (200, json.dumps({"Mounts": [
                        {"Destination": DOCKER_API.DEFAULT_SOCKET_PATH, "RW": True}]}).encode()))
    assert DOCKER_API.is_docker_writable("abc") is True


# --------------------------------------------------------------------------- #
# Self-update orchestration (mocked backend, no daemon)
# --------------------------------------------------------------------------- #

def test_self_update_generator_launches():
    engine = {"n_calls": 0}

    def fake_request(method, path, **kw):
        if path.startswith("/containers/create") and "updater" not in path:
            return {"Id": "n" * 64}
        if path.startswith("/containers/json?all=true"):
            return []
        if path.startswith("/containers/" + "a" * 64):
            return {
                "Config": {"Image": "old", "Env": ["A=1"],
                           "Volumes": {"/data/db": {}, "/app": {}}},
                "HostConfig": {},
                "Mounts": [{"Type": "bind", "Source": "/x/db",
                            "Destination": "/data/db", "RW": True}],
                "NetworkSettings": {"Networks": {"default": {}}},
            }
        if "/start" in path:
            return None
        return {"Id": "zz"}

    DOCKER_API.get_own_container_id = lambda *a, **k: "a" * 64
    DOCKER_API.get_own_container_name = lambda *a, **k: "sdcodex"
    DOCKER_API.is_docker_writable = lambda *a, **k: True
    DOCKER_API.request = fake_request
    DOCKER_API.request_raw = lambda *a, **k: b""

    events = list(SELFUPDATE.self_update_generator("org/app:9.9.9"))
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "launched", kinds
    assert "error" not in kinds, kinds
    launched = [e for e in events if e["event"] == "launched"][0]
    assert launched["data"]["updaterId"] == "zz"


def test_self_update_readonly_error():
    DOCKER_API.get_own_container_id = lambda *a, **k: "a" * 64
    DOCKER_API.is_docker_writable = lambda *a, **k: False
    events = list(SELFUPDATE.self_update_generator("org/app:1"))
    assert events[-1]["event"] == "error"
    assert "read-only" in events[-1]["data"]["message"]


def test_poll_updater_progress_exited():
    DOCKER_API.request = lambda m, p, **kw: {"State": {"Running": False, "ExitCode": 1}}
    DOCKER_API.request_raw = lambda m, p, **kw: b"\x00\x00\x00\x00\x00\x00\x00\x0eUpdater FAILED\n"
    DOCKER_API.decode_container_logs = lambda raw: "Updater FAILED"
    p = SELFUPDATE.poll_updater_progress("zz")
    assert p["status"] == "exited"
    assert p["exit_code"] == 1
    assert "Updater FAILED" in p["logs"]


def test_poll_updater_progress_removed():
    def boom(*a, **k):
        raise DOCKER_API.DockerApiError("gone")
    DOCKER_API.request = boom
    p = SELFUPDATE.poll_updater_progress("gone")
    assert p["status"] == "removed"


# --------------------------------------------------------------------------- #
# Build-context assembly (regression: must never buffer GBs of runtime data)
# --------------------------------------------------------------------------- #

import io as _io
import tarfile as _tarfile


def _make_fake_app(root):
    os.makedirs(os.path.join(root, "app"), exist_ok=True)
    with open(os.path.join(root, "Dockerfile"), "w") as fh:
        fh.write("FROM python:3.11-slim\n")
    with open(os.path.join(root, "app", "routes.py"), "w") as fh:
        fh.write("x = 1\n")


def _tar_names(raw):
    with _tarfile.open(fileobj=_io.BytesIO(raw), mode="r") as tar:
        return tar.getnames()


def test_tar_app_context_excludes_runtime_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SDCODEX_BUILD_MAX_FILE_MB", "1")
    monkeypatch.setenv("SDCODEX_BUILD_MAX_CONTEXT_MB", "256")
    app = str(tmp_path)
    _make_fake_app(app)
    # Runtime data that must never be baked in / read into RAM.
    os.makedirs(os.path.join(app, "models"), exist_ok=True)
    with open(os.path.join(app, "models", "big.safetensors"), "wb") as fh:
        fh.truncate(4 * 1024 * 1024)
    os.makedirs(os.path.join(app, "Huggingface"), exist_ok=True)
    with open(os.path.join(app, "Huggingface", "cache.bin"), "wb") as fh:
        fh.write(b"0" * 16)
    os.makedirs(os.path.join(app, ".git"), exist_ok=True)
    with open(os.path.join(app, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    with open(os.path.join(app, "sdcodex.db"), "wb") as fh:
        fh.write(b"0" * 16)
    with open(os.path.join(app, "huge_weights.pt"), "wb") as fh:
        fh.truncate(2 * 1024 * 1024)  # suffix-excluded (weights) regardless of size
    with open(os.path.join(app, "huge_blob.dat"), "wb") as fh:
        fh.truncate(2 * 1024 * 1024)  # over the 1 MB per-file cap
    with open(os.path.join(app, "notes.log"), "w") as fh:
        fh.write("log\n")

    tar_path, stats = SELFUPDATE._tar_app_context(app)
    try:
        with open(tar_path, "rb") as fh:
            raw = fh.read()
    finally:
        os.unlink(tar_path)
    names = _tar_names(raw)
    assert "Dockerfile" in names
    assert "app/routes.py" in names
    assert not any(n.startswith(("models/", "Huggingface/", ".git/")) for n in names)
    assert "sdcodex.db" not in names
    assert "notes.log" not in names
    assert "huge_weights.pt" not in names
    assert "huge_blob.dat" not in names
    assert any(r == "huge_blob.dat" for r, _ in stats["skipped_large"])
    # Sanity: context stays tiny, not gigabytes.
    assert stats["bytes"] < 1024 * 1024


def test_tar_app_context_total_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SDCODEX_BUILD_MAX_FILE_MB", "8")
    monkeypatch.setenv("SDCODEX_BUILD_MAX_CONTEXT_MB", "16")
    app = str(tmp_path)
    _make_fake_app(app)
    for i in range(18):
        with open(os.path.join(app, f"chunk{i}.dat"), "wb") as fh:
            fh.truncate(1024 * 1024)  # sparse 1 MB files
    with pytest.raises(SELFUPDATE.SelfUpdateError) as excinfo:
        SELFUPDATE._tar_app_context(app)
    assert "exceed" in str(excinfo.value)


def test_build_sdcodex_image_posts_context_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SDCODEX_BUILD_MAX_FILE_MB", "8")
    monkeypatch.setenv("SDCODEX_BUILD_MAX_CONTEXT_MB", "256")
    app = str(tmp_path)
    _make_fake_app(app)
    seen = {}

    def fake_stream(path, tar_path, headers=None, timeout=1200):
        seen["path"] = path
        with open(tar_path, "rb") as fh:
            seen["bytes"] = fh.read()
        yield b'{"stream":"Step 1/2 : FROM python"}'
        yield b'{"stream":"Successfully built abc"}'

    monkeypatch.setattr(DOCKER_API, "post_tar_stream", fake_stream)
    before = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("sdcodex-build-")}
    tag, stats = SELFUPDATE._build_sdcodex_image("local:test", app_root=app)
    after = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("sdcodex-build-")}
    assert tag == "local:test"
    assert stats["files"] >= 2
    assert "/build?" in seen["path"]
    assert "Dockerfile" in _tar_names(seen["bytes"])
    assert after <= before  # temp tar unlinked


def test_stream_build_forwards_heartbeats_as_sentinel(monkeypatch):
    lines = [
        b': ping',
        b'{"stream":"Step 1/2 : FROM python:3.11-slim"}',
        b': ping',
    ]
    monkeypatch.setattr(DOCKER_API, "post_tar_stream",
                        lambda *a, **k: iter(lines))
    out = list(SELFUPDATE._stream_build("local:t", "/tmp/x.tar"))
    assert out[0] is SELFUPDATE._HEARTBEAT
    assert out[2] is SELFUPDATE._HEARTBEAT
    assert any("[Step 1/2]" in m for m in out if isinstance(m, str)), out


def test_stream_build_parses_steps_and_surfaces_errors(monkeypatch):
    lines = [
        b'{"stream":"Step 1/3 : FROM python:3.11-slim"}',
        b'{"stream":"Step 1/3 : FROM python:3.11-slim"}',  # duplicate -> deduped
        b'{"status":"Downloading","progressDetail":{"current":5,"total":10}}',
        b'{"stream":"Successfully built abc123"}',
    ]
    monkeypatch.setattr(DOCKER_API, "post_tar_stream",
                        lambda *a, **k: iter(lines))
    out = list(SELFUPDATE._stream_build("local:t", "/tmp/does-not-matter.tar"))
    assert any("[Step 1/3]" in m for m in out), out

    def boom(*a, **k):
        yield b'{"errorDetail":{"message":"COPY failed: no such file"},"error":"COPY failed"}'
    monkeypatch.setattr(DOCKER_API, "post_tar_stream", boom)
    with pytest.raises(SELFUPDATE.SelfUpdateError) as excinfo:
        list(SELFUPDATE._stream_build("local:t", "/tmp/x.tar"))
    assert "COPY failed" in str(excinfo.value)


def test_cleanup_previous_prunes_stale_sdupdate_images(monkeypatch):
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, path))
        if path == "/containers/json?all=true":
            return []
        if path == "/images/json":
            return [
                {"Id": "sha256:old", "RepoTags": ["sdcodex-selfupdate:sdupdate-111"]},
                {"Id": "sha256:keep", "RepoTags": ["nakedzombie/sdcodex:latest"]},
            ]
        return None

    monkeypatch.setattr(DOCKER_API, "request", fake_request)
    SELFUPDATE.cleanup_previous()
    deletes = [p for m, p in calls if m == "DELETE"]
    assert any("sha256:old" in p for p in deletes), deletes
    assert not any("sha256:keep" in p for p in deletes), deletes


def test_cleanup_previous_keeps_freshly_built_image(monkeypatch):
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, path))
        if path == "/containers/json?all=true":
            return []
        if path == "/images/json":
            return [
                {"Id": "sha256:new", "RepoTags": ["sdcodex-selfupdate:sdupdate-999"]},
                {"Id": "sha256:old", "RepoTags": ["sdcodex-selfupdate:sdupdate-111"]},
            ]
        return None

    monkeypatch.setattr(DOCKER_API, "request", fake_request)
    SELFUPDATE.cleanup_previous(keep_tag="sdcodex-selfupdate:sdupdate-999")
    deletes = [p for m, p in calls if m == "DELETE"]
    assert not any("sha256:new" in p for p in deletes), deletes
    assert any("sha256:old" in p for p in deletes), deletes