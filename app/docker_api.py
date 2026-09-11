"""Minimal Docker Engine API client over a Unix socket (or TCP DOCKER_HOST).

Brings a subset of the Docker HTTP API used by the in-UI "self-update" flow —
enough to:

* detect this container's own ID / name,
* inspect a (this) container and reconstruct a ``docker create`` config,
* pull / build images with streaming progress,
* create containers and start them,
* read a container's logs (Docker log-stream multiplexing) and state.

It talks to the Docker daemon directly. When the host Docker socket is mounted
read-only or not mounted at all, the caller is expected to degrade gracefully
(see ``available()`` / ``writable()``).

This is the Python port of Dockhand's ``src/lib/server/docker.ts`` remote + the
self-update orchestration, adapted to this repo's Flask/requests layout. It does
NOT shell out to a ``docker`` binary (the app container has none), it speaks the
Engine API over the socket instead.
"""

import http.client
import json
import os
import re
import socket

# Where Docker's API is reachable from inside this container.
DEFAULT_SOCKET_PATH = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
# Host/Daemon override scheme: DOCKER_HOST=tcp://host:port
DOCKER_HOST = os.environ.get("DOCKER_HOST") or ""


class DockerApiError(Exception):
    """Raised on non-2xx Docker API responses or connection failures."""


def _tcp_netloc():
    """Return ('host', port) when DOCKER_HOST is a tcp:// URI, else None."""
    if DOCKER_HOST.startswith("tcp://"):
        netloc = DOCKER_HOST[len("tcp://"):].rstrip("/")
        if ":" in netloc:
            host, _, port = netloc.rpartition(":")
            try:
                return host, int(port)
            except ValueError:
                return netloc, 2375
        return netloc, 2375
    return None


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a Unix domain socket instead of TCP."""

    def __init__(self, socket_path, timeout=60):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class _TCPHTTPConnection(http.client.HTTPConnection):
    """Plain HTTPConnection for a tcp:// DOCKER_HOST."""


def request(method, path, body=None, headers=None, timeout=60):
    """Perform a request against the Docker Engine API.

    Returns the decoded JSON body (dict/list) for 2xx responses. Streaming / raw
    binary responses (e.g. image pull, container logs) are handled by the
    dedicated helpers below.
    """
    headers = dict(headers or {})
    payload = None
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = str(body).encode("utf-8")

    tcp = _tcp_netloc()
    if tcp:
        conn = _TCPHTTPConnection(tcp[0], tcp[1], timeout=timeout)
    else:
        conn = _UnixHTTPConnection(DEFAULT_SOCKET_PATH, timeout=timeout)

    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    except (OSError, http.client.HTTPException, socket.timeout) as exc:
        raise DockerApiError(f"Docker API unreachable ({exc})") from exc
    finally:
        conn.close()

    if 200 <= status < 300:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return raw.decode("utf-8", "replace")
    # 404 is a normal "not found" (e.g. removed container); surface status.
    if status == 404:
        raise DockerApiError(f"not found ({method} {path}) {raw[:200]}")
    raise DockerApiError(f"Docker API {status} on {method} {path}: {raw[:300]}")


def request_raw(method, path, body=None, headers=None, timeout=60):
    """Perform a request and return the raw bytes body.

    Used for binary/streaming endpoints (image pulls, container logs, builds)
    where the response is not JSON. Returns ``None`` for empty 2xx bodies.
    """
    headers = dict(headers or {})
    payload = None
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = str(body).encode("utf-8")

    tcp = _tcp_netloc()
    if tcp:
        conn = _TCPHTTPConnection(tcp[0], tcp[1], timeout=timeout)
    else:
        conn = _UnixHTTPConnection(DEFAULT_SOCKET_PATH, timeout=timeout)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
    except (OSError, http.client.HTTPException, socket.timeout) as exc:
        raise DockerApiError(f"Docker API unreachable ({exc})") from exc
    finally:
        conn.close()
    if 200 <= status < 300:
        return raw if raw else None
    if status == 404:
        raise DockerApiError(f"not found ({method} {path}) {raw[:200]}")
    raise DockerApiError(f"Docker API {status} on {method} {path}: {raw[:300]}")


def available():
    """True if the Docker endpoint is reachable at all (mount present)."""
    try:
        request("GET", "/_ping", timeout=3)
        return True
    except DockerApiError:
        return False


def is_docker_writable(container_id=None):
    """Detect whether the socket mount is read-write.

    Over a TCP host we assume write access. Over a socket we inspect our own
    mounts (or the given container's) and check the ``RW`` flag on the socket
    destination. Returns True when writable, False when read-only, and None when
    it can't be determined.
    """
    if _tcp_netloc():
        return True
    cid = container_id or get_own_container_id()
    if not cid:
        return None
    try:
        info = request("GET", f"/containers/{cid}/json")
    except DockerApiError:
        return None
    dest = DEFAULT_SOCKET_PATH
    for m in info.get("Mounts") or []:
        if m.get("Destination") == dest:
            return bool(m.get("RW"))
    return None


# --------------------------------------------------------------------------- #
# Own-container identification
# --------------------------------------------------------------------------- #

_CONTAINER_ID_HEX = re.compile(r"[a-f0-9]{64}")


def get_own_container_id():
    """Heuristic detection of this container's 64-hex ID (Dockhand port)."""
    # 1. cgroup v2 / v1
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as fh:
            m = _CONTAINER_ID_HEX.search(fh.read())
            if m:
                return m.group(0)
    except OSError:
        pass
    # 2. mountinfo
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
            m = re.search(r"/docker/containers/([a-f0-9]{64})", fh.read())
            if m:
                return m.group(1)
    except OSError:
        pass
    # 3. HOSTNAME may be the short container id (12 hex)
    hostname = os.environ.get("HOSTNAME")
    if hostname and re.fullmatch(r"[a-f0-9]{12}", hostname):
        return _expand_short_id(hostname)
    return None


def _expand_short_id(short_id):
    """Best-effort expand a 12-hex short id to the full 64-hex id."""
    try:
        ctrs = request("GET", "/containers/json?all=true")
        for c in ctrs or []:
            if (c.get("Id") or "").startswith(short_id):
                return c["Id"]
    except DockerApiError:
        pass
    return short_id


def get_own_container_name(container_id=None):
    cid = container_id or get_own_container_id()
    if not cid:
        return None
    try:
        info = request("GET", f"/containers/{cid}/json")
        name = info.get("Name") or ""
        return name.lstrip("/") or None
    except DockerApiError:
        return None


# --------------------------------------------------------------------------- #
# Log stream de-multiplexing (Docker /containers/{id}/logs)
# --------------------------------------------------------------------------- #

def decode_container_logs(raw: bytes) -> str:
    """Strip Docker log-stream 8-byte multiplex headers -> plain text.

    Frame header: [stream_type(1)][0(3)][size(4, big-endian)].
    """
    lines = []
    offset = 0
    while offset < len(raw):
        if offset + 8 <= len(raw):
            stream_type = raw[offset]
            if stream_type <= 2:
                size = int.from_bytes(raw[offset + 4:offset + 8], "big")
                end = offset + 8 + size
                if size > 0 and end <= len(raw):
                    chunk = raw[offset + 8:end]
                    for line in chunk.decode("utf-8", "replace").split("\n"):
                        if line.strip():
                            lines.append(line)
                    offset = end
                    continue
        # Fallback: interpret remainder as plain text
        for line in raw[offset:].decode("utf-8", "replace").split("\n"):
            if line.strip():
                lines.append(line)
        break
    return "\n".join(lines)