"""In-UI self-update orchestration (Dockhand-style image swap).

The web UI replaces its own running container entirely from within the UI. It
uses the Docker Engine API over the mounted host socket to:

* pull/build the (new) SDCodex image with progress,
* reconstruct this container's ``docker create`` config from its own inspect
  (rebuilding mounts/binds/ports exactly),
* create a temporary replacement container named ``<name>-updating``,
* launch a minimal updater **sidecar** image that performs the irreversible
  swap: stop old -> rm old -> rename new -> reconnect networks -> start new.

Because the sidecar is a separate container with its own Docker access, stopping
the old SDCodex container does not interrupt the swap. This mirrors Dockhand's
``src/routes/api/self-update/+server.ts`` + ``progress/+server.ts``.

When the socket is absent or read-only the module degrades gracefully: callers
check ``docker_api.available()`` / ``docker_api.is_docker_writable()`` and show a
clear message instead of attempting the swap.
"""

import io
import json
import logging
import os
import re
import subprocess
import tarfile
import time
from urllib.parse import quote

from . import docker_api

logger = logging.getLogger(__name__)

# Image used to create the updater sidecar container. Override with
# SDCODEX_UPDATER_IMAGE if you publish your own.
UPDATER_IMAGE = os.environ.get("SDCODEX_UPDATER_IMAGE", "sdcodex-updater:latest")
UPDATER_LABEL = "sdcodex.updater"
# Label marking the temporary replacement container.
CREATE_LABEL = "sdcodex.selfupdate"
# Sentinel yielded by _stream_build for stream keepalives (daemon quiet).
# Identity-compared; never rendered as a log line — the SSE layer turns it
# into a lightweight "ping" event the frontend ignores (but which keeps the
# HTTP stream alive through long silent build stretches).
_HEARTBEAT = object()


def _default_image():
    return os.environ.get("SDCODEX_IMAGE", "nakedzombie/sdcodex:latest")


class SelfUpdateError(Exception):
    """Fatal, user-facing failure during preparation."""


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #

def _split_image_ref(image):
    """Return ``(repo, tag)`` for an image reference like ``org/app:1.2.3``."""
    if ":" in image:
        last = image.rfind(":")
        candidate = image[last + 1:]
        if "/" not in candidate:
            return image[:last], candidate
    return image, "latest"


def _pull_image(image):
    """Pull an image via /images/create, returning raw progress bytes/None."""
    repo, tag = _split_image_ref(image)
    return docker_api.request_raw(
        "POST", f"/images/create?fromImage={quote(repo, safe='')}&tag={quote(tag, safe='')}",
        body="",
        headers={"Content-Type": "application/json"},
        timeout=600,
    )


def _image_exists(image):
    try:
        docker_api.request("GET", f"/images/{quote(image, safe='/')}/json", timeout=10)
        return True
    except docker_api.DockerApiError:
        return False


def _build_updater_image(ctx):
    """Build the updater image from a Dockerfile dir via the classic /build API."""
    tar_bytes = _tar_directory(ctx)
    docker_api.request_raw(
        "POST",
        f"/build?dockerfile=Dockerfile&t={quote(UPDATER_IMAGE, safe='/')}",
        body=tar_bytes,
        headers={"Content-Type": "application/x-tar"},
        timeout=600,
    )


def _build_sdcodex_image(repo_tag, app_root="/app"):
    """Build a fresh, per-user SDCodex image from this container's live /app
    tree via the classic /build API. Returns ``(image_tag, stats)``. This is
    what makes self-update reflect each user's installed plugins/requirements
    (the build context includes their mounted app/requirements/entrypoint and
    the current Dockerfile), instead of pulling a generic published image."""
    if not os.path.isfile(os.path.join(app_root, "Dockerfile")):
        raise SelfUpdateError(
            f"No Dockerfile found at {app_root}/Dockerfile to build the image from."
        )
    tar_path, stats = _tar_app_context(app_root)
    try:
        _post_build_context(repo_tag, tar_path)
    finally:
        try:
            os.unlink(tar_path)
        except OSError:
            pass
    return repo_tag, stats


def _post_build_context(repo_tag, tar_path):
    """POST a pre-assembled tar build context to the Docker /build API."""
    for _ in _stream_build(repo_tag, tar_path):
        pass


def _stream_build(repo_tag, tar_path):
    """Yield condensed human-readable image-build progress lines.

    Parses the daemon's ``/build`` JSON stream incrementally (so callers can
    forward progress live instead of going silent for the 10+ minutes a full
    image build takes) and raises :class:`SelfUpdateError` if the build
    itself fails — previously a failed build was invisible and treated as
    success because only the HTTP status was checked.
    """
    last_step = None
    for raw in docker_api.post_tar_stream(
        f"/build?dockerfile=Dockerfile&t={quote(repo_tag, safe='/')}",
        tar_path,
        headers={"Content-Type": "application/x-tar"},
        timeout=3600,
    ):
        if raw.strip().startswith(b":"):
            # Stream keepalive (SSE comment) — forward as a ping, not a log.
            yield _HEARTBEAT
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        err = obj.get("error") or (obj.get("errorDetail") or {}).get("message")
        if err:
            raise SelfUpdateError(f"Image build failed: {err}")
        stream = (obj.get("stream") or "").strip()
        status = (obj.get("status") or "").strip()
        text = status or stream
        if not text:
            continue
        step_m = re.match(r"Step\s+(\d+)/(\d+)", text)
        if step_m:
            key = step_m.group(0)
            if key != last_step:
                last_step = key
                rest = text[len(key):].strip(" :")
                yield f"[{key}]{(' ' + rest) if rest else ''}"
            continue
        # Sparse progress: only interesting one-liners, capped in length.
        lowered = text.lower()
        if any(k in lowered for k in ("error", "failed", "warning", "downloading",
                                      "extracting", "naming to", "writing image")):
            yield text[:220]
    if last_step is None:
        yield "Build output received (no step markers parsed)."


# Files that plugin installs re-write per-user and that a git pull must never
# clobber. Marked --skip-worktree so git treats them as frozen (pull leaves the
# local plugin-modified content alone, and upstream edits to them are ignored).
_DYNAMIC_APP_FILES = ("plugin-requirements.txt", "docker-compose.override.yml", ".env")


def _git_pull_app(app_root="/app"):
    """Update the mounted SDCodex repo with `git pull`, freezing the per-user
    dynamic files (docker-compose.override.yml, requirements.txt, .env) so the
    pull never overwrites whatever a plugin install wrote to them. Requires the
    whole repo to be bind-mounted (./:/app). Returns an updated message."""
    git_dir = os.path.join(app_root, ".git")
    if not os.path.isdir(git_dir):
        raise SelfUpdateError(
            "The SDCodex repo is not mounted into the container (no "
            f"{git_dir}). Update docker-compose to bind-mount the whole repo "
            "(e.g. `- ./:/app`) so self-update can `git pull` internally, "
            "then try again."
        )
    for f in _DYNAMIC_APP_FILES:
        p = os.path.join(app_root, f)
        if os.path.exists(p):
            subprocess.run(
                ["git", "-C", app_root, "update-index", "--skip-worktree", f],
                check=False, capture_output=True,
            )
    # Refresh from the default branch via fetch + fast-forward. The container has
    # no SSH client/keys, so if origin is an SSH URL we fetch over HTTPS instead
    # (the SDK repos are public). We never rewrite the persisted remote, so the
    # operator can keep pushing over SSH from the host.
    branch = subprocess.run(
        ["git", "-C", app_root, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    fetch_url = _https_fetch_url(app_root)
    fetched = subprocess.run(
        ["git", "-C", app_root, "fetch", fetch_url, branch or "main"],
        capture_output=True, text=True,
    )
    if fetched.returncode != 0:
        raise SelfUpdateError("git fetch failed:\n" + (fetched.stderr or "").strip())
    merged = subprocess.run(
        ["git", "-C", app_root, "merge", "--ff-only", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merged.returncode != 0:
        # Could be "already up to date" vs "no merge base / conflict".
        head = subprocess.run(
            ["git", "-C", app_root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        try:
            subprocess.run(["git", "-C", app_root, "rev-parse", "--verify", "FETCH_HEAD"],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError:
            raise SelfUpdateError("Could not fetch the remote branch.")
        if not head:
            raise SelfUpdateError("git merge failed:\n" + (merged.stderr or merged.stdout or "").strip())
        return f"Already up to date at {head}"
    head = subprocess.run(
        ["git", "-C", app_root, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    sha = (head.stdout or "").strip()
    return f"Pulled {app_root} → {sha or 'HEAD'}"


_SSH_URL_RE = re.compile(r"^(?:ssh://)?git@([^:]+):(.+)$")


def _https_fetch_url(app_root="/app"):
    """Return an HTTPS URL for ``origin`` that the container can fetch without
    SSH keys. Leaves the stored remote untouched. Falls back to 'origin' itself
    (so HTTPS origins keep working), raising if no URL can be derived."""
    try:
        url = subprocess.run(
            ["git", "-C", app_root, "remote", "get-url", "origin"],
            capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        url = ""
    if not url:
        raise SelfUpdateError("Could not determine the git remote URL (no 'origin').")
    # git@github.com:DeeplabSystems/SDCodex.git -> https://github.com/DeeplabSystems/SDCodex.git
    m = _SSH_URL_RE.match(url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    # git://github.com/... -> https://github.com/...
    if url.startswith("git://"):
        return "https://" + url[len("git://"):]
    # ssh://git@github.com/... -> https://github.com/...
    if url.startswith("ssh://"):
        return "https://" + url[url.rfind("@") + 1:]
    # Already https/http/file — use as-is.
    return url


def _tar_directory(dirpath):
    """Pack a directory (Dockerfile + update.sh) into a tar in memory."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for fname in sorted(os.listdir(dirpath)):
            full = os.path.join(dirpath, fname)
            if not (os.path.isfile(full) or os.path.islink(full)):
                continue
            try:
                data = open(full, "rb").read()
            except OSError:
                continue
            info = tarfile.TarInfo(fname)
            info.size = len(data)
            info.mode = 0o755 if os.access(full, os.X_OK) else 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    out.seek(0)
    return out.getvalue()


# Entries under /app that must never be baked into a per-user image: runtime
# secrets/config, and large/generated directories that are bind-mounted anyway
# (model weights, download dirs, caches, DBs, plugin code). Keep this in sync
# with .gitignore/.dockerignore — anything big enough to OOM the builder (the
# old code read the whole tree into RAM, ballooning gunicorn past 6 GB) or to
# bloat the built image must be listed here.
_BUILD_IGNORE = {
    ".env", "docker-compose.yml", "docker-compose.override.yml", "env.example",
    ".ferrite", ".git", "__pycache__", ".pytest_cache", ".dockerignore",
    "db", "mounts", "config", "tasks", "downloads", "plugins", "updater",
    "models", "MODELS", "Huggingface", "huggingface", "comfyui", "rembg_output",
    ".gallery", "venv", ".venv", "env", "ENV", "node_modules",
    ".vscode", ".idea",
    "static/downloads", "static/uploads", "static/saved_gallery",
    "app/static/downloads", "app/static/uploads", "app/static/saved_gallery",
}
# Runtime-data file types: never bake weights/DBs/archives/logs into the image
# (they live on mounted volumes at runtime).
_BUILD_IGNORE_SUFFIX = (
    ".pyc", ".pyo", ".md",
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".gguf",
    ".db", ".sqlite3", ".sqlite-wal", ".sqlite-shm",
    ".zip", ".tar", ".gz", ".tgz", ".7z",
    ".log",
)


def _max_file_bytes():
    """Largest single file allowed into the build context (rest are skipped)."""
    try:
        mb = int(os.environ.get("SDCODEX_BUILD_MAX_FILE_MB", "") or 8)
    except ValueError:
        mb = 8
    return max(mb, 1) * 1024 * 1024


def _max_context_bytes():
    """Hard cap on the total build-context size (fail loudly instead of OOM)."""
    try:
        mb = int(os.environ.get("SDCODEX_BUILD_MAX_CONTEXT_MB", "") or 256)
    except ValueError:
        mb = 256
    return max(mb, 16) * 1024 * 1024


def _tar_app_context(app_root="/app"):
    """Assemble a clean Docker build context from the live /app tree (the app's
    own code + the user's mounted per-user files), excluding secrets and the
    large bind-mounted media dirs. Streams the tar to a temp file (constant
    memory — never buffers file contents in RAM) and returns
    ``(tar_path, stats)``; the caller must unlink ``tar_path`` when done.

    Directories that are mount points (e.g. the ``${MODELS}`` volume over
    ``app/static/downloads``, plugin volume mounts) are pruned: volumes are
    provided at runtime, never baked in. Files larger than
    ``_max_file_bytes()`` are skipped (recorded in stats). If the total would
    exceed ``_max_context_bytes()`` a ``SelfUpdateError`` is raised instead of
    letting the worker OOM."""
    import tempfile

    def _rel(child_abs):
        return os.path.relpath(child_abs, app_root).replace(os.sep, "/")

    def _pruned(rel):
        # Ignore if the relative path is listed or lives under a listed dir.
        return any(rel == ign or rel.startswith(ign + "/") for ign in _BUILD_IGNORE)

    max_file = _max_file_bytes()
    max_total = _max_context_bytes()
    stats = {"files": 0, "bytes": 0, "skipped_large": [], "skipped_mounts": []}
    largest = []  # (size, rel) of included files, for diagnostics

    fd, tar_path = tempfile.mkstemp(prefix="sdcodex-build-", suffix=".tar")
    os.close(fd)
    try:
        with open(tar_path, "wb") as fh:
            with tarfile.open(fileobj=fh, mode="w") as tar:
                for dirpath, dirnames, filenames in os.walk(app_root):
                    kept = []
                    for d in dirnames:
                        full = os.path.join(dirpath, d)
                        rel = _rel(full)
                        if _pruned(rel):
                            continue
                        # Never descend into mounted volumes: their content is
                        # GBs of runtime data (models, downloads, caches) that
                        # must not be read into RAM or baked into the image.
                        if os.path.ismount(full):
                            stats["skipped_mounts"].append(rel)
                            continue
                        kept.append(d)
                    dirnames[:] = kept
                    for fn in filenames:
                        rel = _rel(os.path.join(dirpath, fn))
                        if _pruned(rel) or fn.endswith(_BUILD_IGNORE_SUFFIX):
                            continue
                        full = os.path.join(dirpath, fn)
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            continue
                        if size > max_file:
                            stats["skipped_large"].append((rel, size))
                            continue
                        if stats["bytes"] + size > max_total:
                            top = sorted(largest, reverse=True)[:5]
                            detail = ", ".join(
                                f"{r} ({s / 1048576:.1f} MB)" for s, r in top
                            )
                            raise SelfUpdateError(
                                f"Build context would exceed {max_total / 1048576:.0f} MB "
                                f"({stats['files']} files, {stats['bytes'] / 1048576:.1f} MB so far). "
                                "Large runtime dirs (models/downloads/caches) must stay on "
                                f"mounted volumes, not baked into the image. Largest files: {detail}."
                            )
                        try:
                            with open(full, "rb") as src:
                                info = tarfile.TarInfo(rel)
                                info.size = size
                                info.mode = 0o755 if os.access(full, os.X_OK) else 0o644
                                info.mtime = int(time.time())
                                tar.addfile(info, src)
                        except OSError:
                            continue
                        stats["files"] += 1
                        stats["bytes"] += size
                        largest.append((size, rel))
    except BaseException:
        try:
            os.unlink(tar_path)
        except OSError:
            pass
        raise
    stats["largest"] = sorted(largest, reverse=True)[:5]
    return tar_path, stats


def _updater_dir_candidates():
    """Directories where the updater Dockerfile might be reachable from inside
    the container (used only as a fallback when the image can't be pulled)."""
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(app_root, "updater"), "/app/updater"]


def ensure_updater_image():
    """Make sure the updater image exists: use present -> pull -> build."""
    if _image_exists(UPDATER_IMAGE):
        return
    try:
        _pull_image(UPDATER_IMAGE)
        return
    except docker_api.DockerApiError:
        pass
    for ctx in _updater_dir_candidates():
        if os.path.isdir(ctx) and os.path.exists(os.path.join(ctx, "Dockerfile")):
            _build_updater_image(ctx)
            return
    raise SelfUpdateError(
        f"Updater image '{UPDATER_IMAGE}' is not present and could not be pulled or "
        "built. Publish it (or an override via SDCODEX_UPDATER_IMAGE) or bind-mount "
        "the repo so the updater/ directory is visible inside the container."
    )


# --------------------------------------------------------------------------- #
# Create-config reconstruction (port of Dockhand buildCreateConfig)
# --------------------------------------------------------------------------- #

def build_create_config(info, new_image):
    config = dict(info.get("Config") or {})
    host_config = dict(info.get("HostConfig") or {})

    create = dict(config)
    create["Image"] = new_image
    create["HostConfig"] = host_config

    # Let the new image's own defaults / fresh identity win.
    for key in ("MacAddress", "Entrypoint", "Cmd", "Hostname"):
        create.pop(key, None)

    mounts = info.get("Mounts") or []
    binds = _rebuild_binds(mounts)
    if binds:
        create["HostConfig"] = dict(create.get("HostConfig") or {})
        create["HostConfig"]["Binds"] = binds

    volumes = dict(config.get("Volumes") or {})
    observed = {m.get("Destination") for m in mounts}
    bind_dests = set()
    for b in binds:
        # Bind format is "src:dest[:mode]"; dest is the container path.
        parts = b.split(":")
        if len(parts) >= 2:
            bind_dests.add(parts[1])
    volumes = {d: v for d, v in volumes.items()
               if d not in observed and d not in bind_dests}
    if volumes:
        create["Volumes"] = volumes
    else:
        create.pop("Volumes", None)

    # No NetworkingConfig: avoid static-IP clashes with the running old container.
    create.pop("NetworkingConfig", None)
    return create


def _rebuild_binds(mounts):
    """Convert inspect ``Mounts`` into HostConfig.Binds 'src:dest:mode' strings.

    Docker Binds format is ``host_src:container_dest[:mode]``. The same host
    dir may legally back two container paths, so dedup is by the full
    string — never by src or dest alone.
    """
    binds = []
    seen = set()
    for m in mounts:
        if m.get("Type") != "bind" or not m.get("Source"):
            continue
        src = m["Source"]
        dest = m.get("Destination")
        if not dest:
            continue
        mode = "rw" if m.get("RW") else "ro"
        b = f"{src}:{dest}:{mode}"
        if b not in seen:
            seen.add(b)
            binds.append(b)
    return binds


def build_network_env(info):
    """Build NETWORKS + NETWORK_OPTS_* env for the sidecar to reconnect networks."""
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    entries = [kv for kv in networks.items() if isinstance(kv[1], dict)]
    if not entries:
        return []
    names = []
    envs = []
    for net_name, nc in entries:
        names.append(net_name)
        opts = []
        ipam = nc.get("IPAMConfig") or {}
        if ipam.get("IPv4Address"):
            opts.append(f"--ip {ipam['IPv4Address']}")
        if ipam.get("IPv6Address"):
            opts.append(f"--ip6 {ipam['IPv6Address']}")
        for a in nc.get("Aliases") or []:
            opts.append(f"--alias {a}")
        for lk in nc.get("Links") or []:
            opts.append(f"--link {lk}")
        if opts:
            safe = re.sub(r"[.\-]", "_", net_name)
            envs.append(f"NETWORK_OPTS_{safe}={' '.join(opts)}")
    envs.insert(0, f"NETWORKS={' '.join(names)}")
    return envs


def cleanup_previous(keep_tag=None):
    """Remove leftover updating containers, stale updater sidecars, and stale
    per-user ``sdupdate-*`` images from earlier attempts (each is gigabytes —
    without this every retry leaks another image onto the daemon disk).

    ``keep_tag`` (e.g. the image tag that was just built) is never deleted —
    without this the post-build cleanup would remove the fresh image and the
    subsequent ``/containers/create`` would fail with ``No such image``."""

    def _keep(tags):
        return bool(keep_tag) and keep_tag in (tags or [])
    try:
        ctrs = docker_api.request("GET", "/containers/json?all=true") or []
    except docker_api.DockerApiError:
        ctrs = []
    for c in ctrs:
        labels = c.get("Labels") or {}
        if not (labels.get(CREATE_LABEL) == "true" or labels.get(UPDATER_LABEL) == "true"):
            continue
        cid = c.get("Id")
        try:
            if c.get("State") == "running" and labels.get(UPDATER_LABEL) == "true":
                try:
                    docker_api.request("POST", f"/containers/{cid}/stop")
                except docker_api.DockerApiError:
                    pass
            docker_api.request("DELETE", f"/containers/{cid}?force=true")
        except docker_api.DockerApiError:
            pass
    # Prune our own stale per-user build images (unique tag prefix, only ever
    # created by this flow). Best-effort: never fail preparation over cleanup.
    try:
        images = docker_api.request("GET", "/images/json") or []
    except docker_api.DockerApiError:
        return
    if not isinstance(images, list):
        return
    for img in images:
        if not isinstance(img, dict):
            continue
        tags = img.get("RepoTags") or []
        if not any(t.startswith("sdcodex-selfupdate:sdupdate-") for t in tags):
            continue
        if _keep(tags):
            continue
        try:
            docker_api.request("DELETE", f"/images/{img.get('Id')}?force=true")
            logger.info("Self-update: pruned stale image %s", tags)
        except docker_api.DockerApiError:
            pass


# --------------------------------------------------------------------------- #
# Orchestration (SSE generator)
# --------------------------------------------------------------------------- #

def self_update_generator(new_image):
    """Yield ``{"event": ..., "data": {...}}`` dicts for preparation + launch.

    Ends with a "launched" event (updater id) on success, or an "error" event.
    """
    new_image = new_image or _default_image()

    def step(name, status, message):
        return {"event": "step", "data": {"step": name, "status": status, "message": message}}

    def log(message):
        return {"event": "log", "data": {"message": message}}

    def error_ev(message):
        return {"event": "error", "data": {"step": "preparation", "message": message}}

    updater_id = None
    try:
        logger.info("Self-update requested (image=%s)", new_image)
        if not docker_api.get_own_container_id():
            raise SelfUpdateError("SDCodex is not running in Docker; cannot self-update.")
        if docker_api.is_docker_writable() is False:
            raise SelfUpdateError(
                "Docker socket is mounted read-only. Self-update requires read-write "
                "access to the host Docker socket (see README)."
            )

        own = docker_api.get_own_container_id()
        name = docker_api.get_own_container_name(own) or f"sdcodex-{own[:12]}"

        # 1) obtain the new image: BUILD a fresh per-user image from this
        # container's live /app state (the user's installed plugins/requirements
        # are part of the build context). Only PULL when the operator explicitly
        # names a different remote image in the field.
        deployed = _default_image()
        build_mode = (not new_image) or (new_image == deployed)
        if build_mode:
            try:
                yield step("pulling_image", "active", "Updating source via git pull...")
                yield log("Marking plugin-managed files (override/env/requirements) as frozen → git pull...")
                pulled = _git_pull_app()
                yield log(pulled)
                logger.info("Self-update: %s", pulled)
            except SelfUpdateError as exc:
                raise exc
            except Exception as exc:  # noqa: BLE001
                raise SelfUpdateError(f"git pull failed: {exc}") from exc

            local_tag = f"sdcodex-selfupdate:sdupdate-{int(time.time())}"
            yield step("pulling_image", "active",
                       f"Building {local_tag} from this container's installed state...")
            yield log(f"Building per-user image {local_tag} (no generic pull)...")
            # Assemble the build context first and report its size *before* the
            # blocking POST, so the UI shows progress instead of sitting on
            # "Preparing..." while Docker builds (which takes minutes).
            try:
                tar_path, stats = _tar_app_context()
            except SelfUpdateError as exc:
                raise exc
            except Exception as exc:  # noqa: BLE001
                raise SelfUpdateError(f"Failed to assemble build context: {exc}") from exc
            mb = stats["bytes"] / 1048576
            summary = f"Build context: {stats['files']} files, {mb:.1f} MB"
            skipped = []
            if stats["skipped_large"]:
                big = sorted(stats["skipped_large"], key=lambda x: -x[1])[:3]
                skipped.append(
                    f"{len(stats['skipped_large'])} large file(s) skipped "
                    f"(e.g. {', '.join(r for r, _ in big)})"
                )
            if stats["skipped_mounts"]:
                skipped.append(
                    f"{len(stats['skipped_mounts'])} mounted volume(s) skipped "
                    f"({', '.join(stats['skipped_mounts'][:3])})"
                )
            if skipped:
                summary += " — " + "; ".join(skipped)
            yield log(summary)
            logger.info("Self-update: %s -> building %s", summary, local_tag)
            yield log("Sending build context to Docker (a full image build takes several minutes)...")
            try:
                for prog in _stream_build(local_tag, tar_path):
                    if prog is _HEARTBEAT:
                        yield {"event": "ping", "data": {}}
                    else:
                        yield log(prog)
            except SelfUpdateError as exc:
                raise exc
            except docker_api.DockerApiError as exc:
                raise SelfUpdateError(f"Image build failed: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise SelfUpdateError(f"Failed to build image: {exc}") from exc
            finally:
                try:
                    os.unlink(tar_path)
                except OSError:
                    pass
            logger.info("Self-update: image %s built", local_tag)
            if not _image_exists(local_tag):
                raise SelfUpdateError(
                    f"Image build finished but '{local_tag}' is not on the daemon. "
                    "The build output above should show the failure; otherwise check "
                    "`docker logs sdcodex` for the daemon's response."
                )
            yield step("pulling_image", "completed", "Image built")
            yield log("Image built from current state")
            new_image = local_tag
        else:
            yield step("pulling_image", "active", f"Pulling {new_image}...")
            yield log(f"Pulling {new_image}...")
            try:
                _pull_image(new_image)
            except docker_api.DockerApiError as exc:
                raise SelfUpdateError(f"Failed to pull {new_image}: {exc}") from exc
            yield step("pulling_image", "completed", "Image pulled")
            yield log("Image pulled")

        # 2) build create config from self-inspect
        yield step("building_config", "active", "Building container config...")
        yield log(f"Inspecting container {own[:12]}...")
        info = docker_api.request("GET", f"/containers/{own}/json")
        create_config = build_create_config(info, new_image)
        net_env = build_network_env(info)
        yield log(f"Networks: {net_env[0] if net_env else 'default'}")
        yield step("building_config", "completed", "Config ready")

        # 3) ensure updater image
        yield step("pulling_updater", "active", "Preparing updater image...")
        ensure_updater_image()
        yield log("Updater image ready")
        yield step("pulling_updater", "completed", "Updater ready")

        # 4) create replacement container (temp name, no networking)
        yield step("creating_container", "active", "Creating new container...")
        yield log("Cleaning up previous updater containers...")
        cleanup_previous(keep_tag=new_image)

        temp_name = f"{name}-updating"
        create_config.setdefault("Labels", {})[CREATE_LABEL] = "true"
        try:
            created = docker_api.request(
                "POST", f"/containers/create?name={quote(temp_name, safe='')}",
                body=create_config,
            )
        except docker_api.DockerApiError as exc:
            raise SelfUpdateError(f"Failed to create container: {exc}") from exc
        new_cont = created.get("Id")
        yield log(f"Container created: {new_cont[:12]} ({temp_name})")
        yield step("creating_container", "completed", "Container created")

        # 5) launch updater sidecar (point of no return)
        yield step("launching_updater", "active", "Launching updater...")
        updater_env = [
            f"OLD_CONTAINER_ID={own}",
            f"NEW_CONTAINER_ID={new_cont}",
            f"CONTAINER_NAME={name}",
            *net_env,
        ]
        if docker_api._tcp_netloc():
            updater_env.append(f"DOCKER_HOST={os.environ.get('DOCKER_HOST') or ''}")
            updater_host = {"AutoRemove": True, "NetworkMode": "host"}
        else:
            sock = docker_api.DEFAULT_SOCKET_PATH
            updater_host = {"AutoRemove": True, "Binds": [f"{sock}:{sock}"]}

        updater = docker_api.request(
            "POST", "/containers/create?name=sdcodex-updater",
            body={
                "Image": UPDATER_IMAGE,
                "Env": updater_env,
                "Labels": {UPDATER_LABEL: "true"},
                "HostConfig": updater_host,
            },
        )
        updater_id = updater.get("Id")
        try:
            docker_api.request("POST", f"/containers/{updater_id}/start")
        except docker_api.DockerApiError as exc:
            docker_api.request("DELETE", f"/containers/{updater_id}?force=true")
            raise SelfUpdateError(f"Failed to start updater: {exc}") from exc

        yield log(f"Updater started: {updater_id[:12]}")
        yield log("Handing off to updater sidecar...")
        yield step("launching_updater", "completed", "Updater launched")
        logger.info("Self-update: handed off to updater %s", updater_id[:12])
        yield {"event": "launched", "data": {"updaterId": updater_id}}
    except SelfUpdateError as exc:
        logger.warning("Self-update failed: %s", exc)
        yield error_ev(str(exc))
    except docker_api.DockerApiError as exc:
        logger.warning("Self-update failed: %s", exc)
        yield error_ev(str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any prep failure
        logger.exception("Self-update preparation crashed")
        if updater_id:
            try:
                docker_api.request("DELETE", f"/containers/{updater_id}?force=true")
            except docker_api.DockerApiError:
                pass
        yield error_ev(str(exc))


# --------------------------------------------------------------------------- #
# Progress polling (mirror progress/+server.ts)
# --------------------------------------------------------------------------- #

def poll_updater_progress(container_id):
    """Return ``{"logs", "status", "exit_code"}`` for the updater sidecar."""
    if not container_id:
        return {"logs": "", "status": "removed", "exit_code": 0}
    try:
        info = docker_api.request("GET", f"/containers/{container_id}/json")
    except docker_api.DockerApiError:
        return {"logs": "", "status": "removed", "exit_code": 0}
    st = info.get("State") or {}
    status = "running" if st.get("Running") else "exited"
    exit_code = st.get("ExitCode") or 0
    try:
        raw = docker_api.request_raw(
            "GET",
            f"/containers/{container_id}/logs?stdout=true&stderr=true&timestamps=false",
        )
    except docker_api.DockerApiError:
        raw = b""
    logs = docker_api.decode_container_logs(raw or b"")
    return {"logs": logs, "status": status, "exit_code": exit_code}