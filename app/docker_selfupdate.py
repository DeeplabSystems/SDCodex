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
import os
import re
import subprocess
import tarfile
import time
from urllib.parse import quote

from . import docker_api

# Image used to create the updater sidecar container. Override with
# SDCODEX_UPDATER_IMAGE if you publish your own.
UPDATER_IMAGE = os.environ.get("SDCODEX_UPDATER_IMAGE", "sdcodex-updater:latest")
UPDATER_LABEL = "sdcodex.updater"
# Label marking the temporary replacement container.
CREATE_LABEL = "sdcodex.selfupdate"


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
    tree via the classic /build API. Returns the image tag. This is what makes
    self-update reflect each user's installed plugins/requirements (the build
    context includes their mounted app/requirements/entrypoint and the current
    Dockerfile), instead of pulling a generic published image."""
    if not os.path.isfile(os.path.join(app_root, "Dockerfile")):
        raise SelfUpdateError(
            f"No Dockerfile found at {app_root}/Dockerfile to build the image from."
        )
    tar_bytes = _tar_app_context(app_root)
    docker_api.request_raw(
        "POST",
        f"/build?dockerfile=Dockerfile&t={quote(repo_tag, safe='/')}",
        body=tar_bytes,
        headers={"Content-Type": "application/x-tar"},
        timeout=1200,
    )
    return repo_tag


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
    # Refresh from the default branch via fetch + fast-forward. Pulling by an
    # explicit remote ref is robust whether or not a tracking branch is set.
    branch = subprocess.run(
        ["git", "-C", app_root, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    fetched = subprocess.run(
        ["git", "-C", app_root, "fetch", "origin"],
        capture_output=True, text=True,
    )
    if fetched.returncode != 0:
        raise SelfUpdateError("git fetch failed:\n" + (fetched.stderr or "").strip())
    ref = f"origin/{branch}" if branch and branch != "HEAD" else "origin/main"
    merged = subprocess.run(
        ["git", "-C", app_root, "merge", "--ff-only", ref],
        capture_output=True, text=True,
    )
    if merged.returncode != 0:
        # Could be nothing to merge (already up to date) vs a real conflict.
        try:
            subprocess.run(["git", "-C", app_root, "rev-parse", "--verify", ref],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError:
            raise SelfUpdateError(f"Remote branch '{ref}' not found on origin.")
        # If we're already at the remote tip that's fine (already current).
        head = subprocess.run(
            ["git", "-C", app_root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        return f"Already up to date at {head or 'HEAD'}"
    head = subprocess.run(
        ["git", "-C", app_root, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    sha = (head.stdout or "").strip()
    return f"Pulled {app_root} → {sha or 'HEAD'}"


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
# secrets/config, and large/generated directories that are bind-mounted anyway.
_BUILD_IGNORE = {
    ".env", "docker-compose.yml", "docker-compose.override.yml", "env.example",
    ".ferrite", ".git", "__pycache__", ".pytest_cache", ".dockerignore",
    "db", "mounts", "config", "tasks", "downloads", "plugins", "updater",
    "static/downloads", "static/uploads", "static/saved_gallery",
}
_BUILD_IGNORE_SUFFIX = (".pyc", ".pyo", ".md")


def _tar_app_context(app_root="/app"):
    """Assemble a clean Docker build context from the live /app tree (the app's
    own code + the user's mounted per-user files), excluding secrets and the
    large bind-mounted media dirs. Used so self-update *builds* an image that
    reflects each user's installed plugins/requirements rather than pulling a
    generic published image."""
    files = []

    def _rel(child_abs):
        return os.path.relpath(child_abs, app_root).replace(os.sep, "/")

    def _pruned(rel):
        # Ignore if the whole relative path is listed, or its top component is.
        parts = rel.split("/")
        return rel in _BUILD_IGNORE or parts[0] in _BUILD_IGNORE

    for dirpath, dirnames, filenames in os.walk(app_root):
        dirnames[:] = [d for d in dirnames if not _pruned(_rel(os.path.join(dirpath, d)))]
        for fn in filenames:
            rel = _rel(os.path.join(dirpath, fn))
            if _pruned(rel) or fn.endswith(_BUILD_IGNORE_SUFFIX):
                continue
            full = os.path.join(dirpath, fn)
            try:
                data = open(full, "rb").read()
            except OSError:
                continue
            files.append((rel, data, os.access(full, os.X_OK)))
    files.sort(key=lambda x: x[0])
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for rel, data, xok in files:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if xok else 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    out.seek(0)
    return out.getvalue()


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
    volumes = {d: v for d, v in volumes.items()
               if d not in observed and d not in {b[0] for b in binds}}
    if volumes:
        create["Volumes"] = volumes
    else:
        create.pop("Volumes", None)

    # No NetworkingConfig: avoid static-IP clashes with the running old container.
    create.pop("NetworkingConfig", None)
    return create


def _rebuild_binds(mounts):
    """Convert inspect ``Mounts`` into HostConfig.Binds 'dest:src:rw' strings."""
    binds = []
    for m in mounts:
        if m.get("Type") != "bind" or not m.get("Source"):
            continue
        src = m["Source"]
        dest = m.get("Destination")
        if not dest:
            continue
        mode = "rw" if m.get("RW") else "ro"
        binds.append(f"{dest}:{src}:{mode}")
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


def cleanup_previous():
    """Remove leftover updating containers and stale updater sidecars."""
    try:
        ctrs = docker_api.request("GET", "/containers/json?all=true") or []
    except docker_api.DockerApiError:
        return
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
            except SelfUpdateError as exc:
                raise exc
            except Exception as exc:  # noqa: BLE001
                raise SelfUpdateError(f"git pull failed: {exc}") from exc

            local_tag = f"sdcodex-selfupdate:sdupdate-{int(time.time())}"
            yield step("pulling_image", "active",
                       f"Building {local_tag} from this container's installed state...")
            yield log(f"Building per-user image {local_tag} (no generic pull)...")
            try:
                _build_sdcodex_image(local_tag)
            except Exception as exc:
                raise SelfUpdateError(f"Failed to build image: {exc}") from exc
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
        cleanup_previous()

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
        yield {"event": "launched", "data": {"updaterId": updater_id}}
    except SelfUpdateError as exc:
        yield error_ev(str(exc))
    except docker_api.DockerApiError as exc:
        yield error_ev(str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any prep failure
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