from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    current_app,
    session,
    send_file,
)
from app import api
from app import db
from app.models import Setting, Download, PluginRepo
from app.download_manager import download_manager
from app.plugin_manager import plugin_manager
from app import docker_api
from app import docker_selfupdate
from app import auth
from app import oidc as oidc_mod
from app.models import Setting, Download, PluginRepo, User, OidcConfig
from flask_paginate import Pagination, get_page_parameter
import os
import re
import json
import uuid

main = Blueprint("main", __name__)

@main.record
def record(state):
    download_manager.init_app(state.app)

@main.route("/")
def index():
    # Get NSFW filter parameter
    nsfw = request.args.get("nsfw", "false")

    # Get random models from the API
    try:
        params = {"limit": 10, "nsfw": nsfw == "true"}
        response = api.get_models(params, api_key=session.get("api_key"))
        random_models = response.get("items", [])
    except Exception as e:
        flash(f"Error fetching models from API: {e}", "error")
        random_models = []

    # Get featured images from the models
    featured_images = []
    for model in random_models:
        if model.get("modelVersions"):
            for version in model["modelVersions"]:
                if version.get("images"):
                    for img in version["images"]:
                        img_with_id = dict(img)
                        img_with_id["model_id"] = model.get("id")
                        featured_images.append(img_with_id)
    featured_images = featured_images[:5]

    # Get model types and base models from the API response
    model_types = list(set(model["type"] for model in random_models if model.get("type")))
    base_models = list(
        set(
            version["baseModel"]
            for model in random_models
            if model.get("modelVersions")
            for version in model["modelVersions"]
            if version.get("baseModel")
        )
    )

    return render_template(
        "index.html",
        random_models=random_models,
        featured_images=featured_images,
        model_types=model_types,
        base_models=base_models,
        current_nsfw=nsfw,
    )

MODEL_TYPES = [
    "Checkpoint",
    "Embedding",
    "Hypernetwork",
    "AestheticGradient",
    "LORA",
    "LyCORIS",
    "DoRA",
    "Controlnet",
    "Upscaler",
    "Motion",
    "VAE",
    "Poses",
    "Wildcards",
    "Workflows",
    "Detection",
    "Other",
]

SORT_OPTIONS = [
    ("Highest Rated", "Highest Rated"),
    ("Most Downloaded", "Most Downloaded"),
    ("Newest", "Newest"),
]

PERIOD_OPTIONS = [
    ("Day", "Day"),
    ("Week", "Week"),
    ("Month", "Month"),
    ("Year", "Year"),
    ("AllTime", "All Time"),
]

BASE_MODELS = [
    "Aura Flow",
    "Chroma",
    "CogVideoX",
    "Flux.1 S",
    "Flux.1 D",
    "Flux.1 Krea",
    "Flux.1 Kontext",
    "HiDream",
    "Hunyuan 1",
    "Hunyuan Video",
    "Illustrious",
    "Kolors",
    "LTXV",
    "Lumina",
    "Mochi",
    "NoobAI",
    "Other",
    "PixArt a",
    "PixArt Σ",
    "Pony",
    "Pony V7",
    "Qwen",
    "SD 1.4",
    "SD 1.5",
    "SD 1.5 LCM",
    "SD 1.5 Hyper",
    "SD 2.0",
    "SD 2.1",
    "SDXL 1.0",
    "SDXL Lightning",
    "SDXL Hyper",
    "Wan Video 1.3B t2v",
    "Wan Video 14B t2v",
    "Wan Video 14B i2v 480p",
    "Wan Video 14B i2v 720p",
    "Wan Video 2.2 TI2V-5B",
    "Wan Video 2.2 I2V-A14B",
    "Wan Video 2.2 T2V-A14B",
    "Wan Video 2.5 T2V",
    "Wan Video 2.5 I2V",
]

CHECKPOINT_TYPE_OPTIONS = [
    ("All", "All"),
    ("Trained", "Trained"),
    ("Merge", "Merge"),
]

FILE_FORMAT_OPTIONS = [
    ("SafeTensor", "SafeTensor"),
    ("PickleTensor", "PickleTensor"),
    ("GGUF", "GGUF"),
    ("Diffusers", "Diffusers"),
    ("Core ML", "Core ML"),
    ("ONNX", "ONNX"),
]

MODEL_STATUS_OPTIONS = [
    ("EarlyAccess", "Early Access"),
    ("OnSiteGeneration", "On-site Generation"),
    ("Featured", "Featured"),
]

@main.route("/models")
def models():
    page = request.args.get(get_page_parameter(), type=int, default=1)
    per_page = current_app.config["MODELS_PER_PAGE"]

    type_filter = request.args.get("type")
    base_model_filter = request.args.get("base_model")
    sort_by = request.args.get("sort", "Newest")
    period = request.args.get("period", "AllTime")
    nsfw = request.args.get("nsfw", "false")
    tags = request.args.getlist("tags")
    
    checkpoint_type = request.args.get("checkpoint_type")
    file_format = request.args.get("format")
    status = request.args.get("status")

    try:
        params = {
            "page": page,
            "limit": per_page,
            "types": type_filter,
            "sort": sort_by,
            "period": period,
            "nsfw": nsfw == "true",
            "tags": ",".join(tags),
        }
        if base_model_filter:
            params["baseModels"] = base_model_filter
            
        if checkpoint_type and checkpoint_type != "All":
            params["checkpointType"] = checkpoint_type
            
        if file_format:
            params["format"] = file_format
            
        if status:
            if status == "EarlyAccess":
                params["earlyAccess"] = "true"
            elif status == "OnSiteGeneration":
                params["supportsGeneration"] = "true"
            elif status == "Featured":
                params["featured"] = "true"

        response = api.get_models(params, api_key=session.get("api_key"))
        models = response.get("items", [])
        total = response.get("metadata", {}).get("totalItems", 0)
    except Exception as e:
        flash(f"Error fetching models from API: {e}", "error")
        models = []
        total = 0

    pagination = Pagination(
        page=page, per_page=per_page, total=total, css_framework="bootstrap4"
    )

    try:
        tags_params = {"limit": 10, "sort": "Most Models"}
        tags_response = api.get_tags(tags_params, api_key=session.get("api_key"))
        popular_tags = [tag["name"] for tag in tags_response.get("items", [])]
    except Exception as e:
        popular_tags = []

    return render_template(
        "models.html",
        models=models,
        pagination=pagination,
        model_types=MODEL_TYPES,
        sort_options=SORT_OPTIONS,
        period_options=PERIOD_OPTIONS,
        base_models=BASE_MODELS,
        checkpoint_type_options=CHECKPOINT_TYPE_OPTIONS,
        file_format_options=FILE_FORMAT_OPTIONS,
        model_status_options=MODEL_STATUS_OPTIONS,
        popular_tags=popular_tags,
        current_filters={
            "type": type_filter,
            "base_model": base_model_filter,
            "sort": sort_by,
            "period": period,
            "nsfw": nsfw,
            "tags": tags,
            "checkpoint_type": checkpoint_type,
            "format": file_format,
            "status": status,
        },
    )

@main.route("/models/<int:model_id>")
@main.route("/model/<int:model_id>")
def model_detail(model_id):
    try:
        model = api.get_model(model_id, api_key=session.get("api_key"))
    except Exception as e:
        flash(f"Error fetching model from API: {e}", "error")
        return redirect(url_for("main.index"))

    versions = model.get("modelVersions", [])
    preview_images = []
    for version in versions:
        preview_images.extend(version.get("images", []))

    tags = model.get("tags", [])
    similar_models = []
    related_by_type = []
    model_types = []

    return render_template(
        "model_detail.html",
        model=model,
        versions=versions,
        preview_images=preview_images,
        tags=tags,
        similar_models=similar_models,
        related_by_type=related_by_type,
        model_types=model_types,
    )

@main.route("/creators")
def creators():
    page = request.args.get(get_page_parameter(), type=int, default=1)
    per_page = current_app.config.get("CREATORS_PER_PAGE", 20)
    sort_by = request.args.get("sort", "most-models")
    query = request.args.get("query", "")

    try:
        params = {"page": page, "limit": per_page, "sort": sort_by}
        if query:
            params["query"] = query
        response = api.get_creators(params, api_key=session.get("api_key"))
        creators = response.get("items", [])
        total = response.get("metadata", {}).get("totalItems", 0)
    except Exception as e:
        flash(f"Error fetching creators from API: {e}", "error")
        creators = []
        total = 0

    pagination = Pagination(
        page=page, per_page=per_page, total=total, css_framework="bootstrap4"
    )

    return render_template(
        "creators.html",
        creators=creators,
        pagination=pagination,
        current_sort=sort_by,
        model_types=[],
        query=query,
    )

@main.route("/creators/<int:creator_id>")
@main.route("/creator/<string:username>")
def creator_detail(creator_id=None, username=None):
    if creator_id is not None:
        try:
            creator = api.get_creator(creator_id, api_key=session.get("api_key"))
        except Exception as e:
            flash(f"Error fetching creator from API: {e}", "error")
            return redirect(url_for("main.creators"))
        uname = creator.get("username")
    else:
        creator = {"username": username}
        uname = username

    try:
        params = {"username": uname}
        response = api.get_models(params, api_key=session.get("api_key"))
        models = response.get("items", [])
    except Exception as e:
        flash(f"Error fetching models from API: {e}", "error")
        models = []

    model_count = len(models)
    download_count = sum(model.get("stats", {}).get("downloadCount", 0) for model in models)

    return render_template(
        "creator_detail.html",
        creator=creator,
        models=models,
        model_count=model_count,
        download_count=download_count,
        model_types=[],
    )

@main.route("/search")
def search():
    query = request.args.get("q", "") or request.args.get("query", "")
    base_model_filter = request.args.get("base_model", "")
    nsfw = request.args.get("nsfw", "false")
    page = request.args.get(get_page_parameter(), type=int, default=1)
    per_page = current_app.config["MODELS_PER_PAGE"]

    if not query:
        return redirect(url_for("main.index"))

    try:
        params = {
            "page": page,
            "limit": per_page,
            "query": query,
            "baseModels": base_model_filter,
            "nsfw": nsfw == "true",
        }
        response = api.get_models(params, api_key=session.get("api_key"))
        models = response.get("items", [])
        total = response.get("metadata", {}).get("totalItems", 0)
    except Exception as e:
        flash(f"Error fetching models from API: {e}", "error")
        models = []
        total = 0

    model_pagination = Pagination(
        page=page, per_page=per_page, total=total, css_framework="bootstrap4"
    )

    try:
        params = {"query": query, "limit": 5}
        response = api.get_creators(params, api_key=session.get("api_key"))
        creators = response.get("items", [])
    except Exception as e:
        creators = []

    base_models = []
    model_types = []

    return render_template(
        "search.html",
        query=query,
        models=models,
        model_pagination=model_pagination,
        creators=creators,
        base_models=base_models,
        model_types=model_types,
        current_base_model=base_model_filter,
        current_nsfw=nsfw,
    )

@main.route("/settings", methods=["GET", "POST"])
def settings():
    active_tab = request.args.get("tab", "download-dirs")

    if request.method == "POST":
        action = request.form.get("action", "")

        # Auth-related settings actions (create/edit users, OIDC providers, toggle).
        if action.startswith("auth:"):
            if _auth_settings_actions(action):
                active_tab = "system-auth"
                return redirect(url_for("main.settings", tab=active_tab) + f"#{active_tab}")

        # 1. API Key Actions
        if action == "save_api_key":
            api_key = request.form.get("api_key", "").strip()
            # Persist per-user when an authenticated session is present.
            current_user = auth.current_user() if auth.get_session_token_from_request() else None
            if api_key:
                if current_user:
                    current_user.api_key = api_key
                    db.session.commit()
                session["api_key"] = api_key
                user = api.get_user(api_key)
                if user:
                    session["user"] = user
                    flash(f"Logged in as {user.get('username', 'Verified User')}", "success")
                else:
                    session.pop("user", None)
                    flash("API Key saved, but could not verify user with Civitai.", "warning")
            else:
                flash("API Key cannot be empty.", "warning")
            active_tab = "download-dirs"

        elif action == "clear_api_key":
            current_user = auth.current_user() if auth.get_session_token_from_request() else None
            if current_user:
                current_user.api_key = ""
                db.session.commit()
            session.pop("api_key", None)
            session.pop("user", None)
            flash("Civitai API Key cleared.", "info")
            active_tab = "download-dirs"

        # 2. Download Directories
        elif action == "save_directories":
            for model_type in MODEL_TYPES:
                dir_key = f"dir_{model_type}"
                dir_val = request.form.get(dir_key, "").strip()
                setting = Setting.query.get(dir_key)
                if not setting:
                    setting = Setting(key=dir_key)
                    db.session.add(setting)
                setting.value = dir_val

            all_settings = Setting.query.all()
            for s in all_settings:
                if s.key.startswith("dir_custom_"):
                    label = s.key[len("dir_custom_"):]
                    val = request.form.get(f"custom_dir_{label}", "").strip()
                    s.value = val

            db.session.commit()
            flash("Download directory settings saved.", "success")
            active_tab = "download-dirs"

        elif action == "add_custom_dir":
            label = request.form.get("new_custom_dir_label", "").strip()
            folder_path = request.form.get("new_custom_dir_path", "").strip()
            if label and folder_path:
                clean_label = re.sub(r'[^a-zA-Z0-9_\-]', '_', label)
                key = f"dir_custom_{clean_label}"
                setting = Setting.query.get(key)
                if not setting:
                    setting = Setting(key=key)
                    db.session.add(setting)
                setting.value = folder_path
                db.session.commit()
                flash(f"Created custom download directory entry '{clean_label}'.", "success")
            else:
                flash("Label and Folder Path are required for custom directory entry.", "error")
            active_tab = "download-dirs"

        elif action.startswith("delete_custom_dir:"):
            label_to_del = action.split(":", 1)[1]
            key = f"dir_custom_{label_to_del}"
            setting = Setting.query.get(key)
            if setting:
                db.session.delete(setting)
                db.session.commit()
                flash(f"Deleted custom download directory entry '{label_to_del}'.", "info")
            active_tab = "download-dirs"

        # 3. Plugin Management Actions
        elif action == "save_github_token":
            token = request.form.get("github_token", "").strip()
            root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_file = os.path.join(root_dir, ".env")
            plugin_manager._update_env_file(env_file, {"GITHUB_TOKEN": token})
            os.environ["GITHUB_TOKEN"] = token
            flash("GitHub Personal Access Token saved successfully to .env!", "success")
            active_tab = "plugins"

        elif action == "add_plugin_repo":
            repo_url = request.form.get("repo_url", "").strip()
            if repo_url:
                short_name, canonical_url = plugin_manager.normalize_github_url(repo_url)
                existing = PluginRepo.query.filter_by(repo_url=canonical_url).first()
                if existing:
                    flash("Repository is already in your repository list.", "warning")
                else:
                    remote_info = plugin_manager.fetch_remote_manifest(canonical_url)
                    name = remote_info.get("name", short_name) if remote_info else short_name
                    desc = remote_info.get("description", "GitHub Plugin Repository") if remote_info else "GitHub Plugin Repository"
                    new_repo = PluginRepo(repo_url=canonical_url, name=name, description=desc)
                    db.session.add(new_repo)
                    db.session.commit()
                    flash(f"Plugin repository '{name}' added successfully!", "success")
            else:
                flash("Please provide a valid GitHub repository URL or owner/repo.", "error")
            active_tab = "plugins"

        elif action.startswith("remove_plugin_repo:"):
            repo_id = int(action.split(":", 1)[1])
            repo = PluginRepo.query.get(repo_id)
            if repo:
                db.session.delete(repo)
                db.session.commit()
                flash(f"Removed repository '{repo.name or repo.repo_url}'.", "info")
            active_tab = "plugins"

        elif action == "check_plugin_updates":
            result = plugin_manager.check_updates()
            updates = result.get("update_cache", {})
            new_plugins = result.get("new_plugins", [])
            store_meta = result.get("store", {})
            has_updates = any(v.get("has_update") for v in updates.values())

            bits = []
            if has_updates:
                bits.append(f"{sum(1 for v in updates.values() if v.get('has_update'))} plugin update(s) available")
            elif store_meta.get("reachable"):
                bits.append("All installed plugins are up to date")
            else:
                bits.append("Store could not be reached")
            if new_plugins:
                bits.append(f"{len(new_plugins)} new plugin(s) available in the store")
            flash("Plugin update check complete: " + "; ".join(bits) + ".", "info" if (has_updates or new_plugins) else "success")
            active_tab = "plugins"

        elif action == "install_plugin":
            repo_url = request.form.get("repo_url", "").strip()
            volume_paths = {}
            for k, v in request.form.items():
                if k.startswith("vol_"):
                    env_var = k[4:]
                    volume_paths[env_var] = v.strip()

            success, msg = plugin_manager.install_plugin(repo_url, volume_paths)
            if success:
                flash(f"{msg} Notice: Volume paths written to .env and docker-compose.yml. Run 'update.sh' to rebuild and mount the new volumes.", "success")
            else:
                flash(f"Failed to install plugin: {msg}", "error")
            active_tab = "plugins"

        elif action.startswith("update_plugin:"):
            plugin_id = action.split(":", 1)[1]
            success, msg = plugin_manager.update_plugin(plugin_id)
            if success:
                flash(f"{msg} Please restart your container if necessary.", "success")
            else:
                flash(f"Update failed: {msg}", "error")
            active_tab = "plugins"

        elif action.startswith("uninstall_plugin:"):
            plugin_id = action.split(":", 1)[1]
            success, msg = plugin_manager.uninstall_plugin(plugin_id)
            if success:
                flash(f"{msg} Compose volumes cleaned up. Run 'update.sh' to restart without this plugin.", "info")
            else:
                flash(f"Uninstall failed: {msg}", "error")
            active_tab = "plugins"

        elif action.startswith("save_plugin_volumes:"):
            plugin_id = action.split(":", 1)[1]
            manifest = plugin_manager.loaded_plugins.get(plugin_id, {})
            volume_paths = {}
            for k, v in request.form.items():
                if k.startswith(f"vol_{plugin_id}_"):
                    env_var = k[len(f"vol_{plugin_id}_"):]
                    volume_paths[env_var] = v.strip()

            plugin_manager.apply_volume_config(manifest, volume_paths)
            flash(f"Volume settings saved for '{plugin_id}'. Run 'update.sh' to apply new mount points.", "success")
            active_tab = "plugins"

        elif action.startswith("toggle_plugin:"):
            plugin_id = action.split(":", 1)[1]
            manifest = plugin_manager.loaded_plugins.get(plugin_id, {})
            currently_enabled = manifest.get("_enabled", True)
            plugin_manager.toggle_plugin(plugin_id, not currently_enabled)
            status_str = "disabled" if currently_enabled else "enabled"
            flash(f"Plugin '{plugin_id}' has been {status_str}.", "info")
            active_tab = "plugins"

        elif action == "system_selfupdate:request":
            # Start the in-UI self-update: pull the image, pre-create a
            # replacement container, and hand off to the updater sidecar. Uses
            # the Docker Engine API over the mounted socket (no host process).
            try:
                image = (request.form.get("self_update_image") or "").strip() or None
                ok, message = _start_self_update(image)
                flash(message, "success" if ok else "error")
            except Exception as e:
                current_app.logger.exception("Failed to start in-UI self-update")
                flash(f"Self-update failed to start: {e}", "error")
            active_tab = "system-update"

        return redirect(url_for("main.settings", tab=active_tab) + f"#{active_tab}")

    # GET Request context
    api_key = session.get("api_key")
    user = session.get("user")

    directories = {}
    for model_type in MODEL_TYPES:
        setting = Setting.query.get(f"dir_{model_type}")
        directories[model_type] = setting.value if setting else ""

    custom_directories = {}
    all_settings = Setting.query.all()
    for s in all_settings:
        if s.key.startswith("dir_custom_"):
            label = s.key[len("dir_custom_"):]
            custom_directories[label] = s.value or ""

    plugin_repos = PluginRepo.query.order_by(PluginRepo.created_at.desc()).all()
    installed_plugins = plugin_manager.get_installed_plugins()
    available_plugins = plugin_manager.get_available_uninstalled_plugins()

    env_values = {}
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in [".env", "env"]:
        env_file = os.path.join(root_dir, fname)
        if os.path.exists(env_file) and not os.path.isdir(env_file):
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env_values[k.strip()] = v.strip()
                break
            except Exception:
                pass

    github_token = plugin_manager._get_github_token() or env_values.get("GITHUB_TOKEN", "")

    # Plugin store status (lightweight: no per-plugin version lookups on GET).
    store_catalog = plugin_manager.get_store_catalog()
    store_info = plugin_manager.store_info
    store_info = {
        "store": store_catalog["store"],
        "plugins": store_catalog["plugins"],
        "new_plugins": store_info.get("new_plugins", []),
        "last_checked": store_info.get("last_checked", ""),
    }

    self_update_info = _self_update_info()

    # Auth context for the Users & SSO tab (only includes auth details for admins
    # or when auth is off to avoid leaking user lists to non-admins).
    auth_ctx = _inject_auth_ctx()
    users = []
    oidc_configs = oidc_mod.all_configs()
    if (not auth.is_auth_enabled()) or (auth_ctx.get("user") and auth_ctx["user"].is_admin):
        users = User.query.order_by(User.username).all()

    return render_template(
        "settings.html",
        api_key=api_key,
        user=user,
        model_types=MODEL_TYPES,
        directories=directories,
        custom_directories=custom_directories,
        plugin_repos=plugin_repos,
        installed_plugins=installed_plugins,
        available_plugins=available_plugins,
        env_values=env_values,
        github_token=github_token,
        store_info=store_info,
        store_url=plugin_manager.get_store_url(),
        self_update_info=self_update_info,
        users=users,
        oidc_configs=oidc_configs,
        auth_enabled=auth.is_auth_enabled(),
        active_tab=active_tab,
    )

@main.route("/download/<int:model_id>/<int:version_id>")
def download(model_id, version_id):
    api_key = session.get("api_key")
    if not api_key:
        flash("You must be logged in to download models.", "warning")
        return redirect(url_for("main.settings"))

    download_manager.add_task(model_id, version_id, api_key)
    flash("Download added to queue.", "info")
    return redirect(request.referrer or url_for("main.model_detail", model_id=model_id))

@main.route("/api/downloads/status")
def download_status():
    return jsonify(download_manager.get_status())

@main.route("/settings/scan", methods=["POST"])
def scan_library():
    api_key = session.get("api_key")
    download_manager.add_task(task_type='scan', api_key=api_key)
    flash("Library scan started in background.", "info")
    return redirect(url_for("main.settings", tab="library") + "#library")

def _self_update_info():
    """Capability summary for the self-update card (cheap, no swap)."""
    uid = docker_api.get_own_container_id()
    available = docker_api.available()
    writable = docker_api.is_docker_writable(uid) if uid else None
    info = {
        "container_id": uid,
        "container_name": docker_api.get_own_container_name(uid) if uid else None,
        "available": bool(available),
        "writable": bool(writable),
        "image": os.environ.get("SDCODEX_IMAGE", docker_selfupdate._default_image()),
        "updater_image": docker_selfupdate.UPDATER_IMAGE,
        "reason": None,
    }
    if not uid:
        info["reason"] = "Not running in Docker (cannot self-update)."
    elif available is False:
        info["reason"] = "Docker socket not reachable from the container."
    elif writable is False:
        info["reason"] = (
            "Docker socket is mounted read-only. Self-update requires read-write "
            "access to the host Docker socket (see README)."
        )
    elif writable is None:
        info["reason"] = "Could not determine Docker socket write access."
    return info

def _start_self_update(image=None):
    """Kick off the self-update preparation; returns (ok, message).

    The actual replacement happens asynchronously: preparation runs in this
    request and swaps on the host via the updater sidecar. We run a short
    synchronous prep here and return the launched state (or raise on failure).
    """
    info = _self_update_info()
    if not info["writable"]:
        return False, "Cannot self-update: " + (info["reason"] or "socket not writable.")
    gen = docker_selfupdate.self_update_generator(image)
    last = None
    for ev in gen:
        # Drain preparation; the client uses the SSE endpoint for live progress.
        last = ev
    if last and last.get("event") == "launched":
        return True, "Self-update launched: the updater sidecar is swapping your container."
    if last and last.get("event") == "error":
        return False, "Self-update failed: " + str(last["data"].get("message", ""))
    return False, "Self-update did not launch (no response from the updater)."

@main.route("/settings/self_update/info", methods=["GET"])
def self_update_info():
    return jsonify(_self_update_info())

@main.route("/settings/self_update", methods=["POST"])
def self_update():
    """SSE stream for self-update preparation + launch.

    Fail-fast JSON when the socket is missing/read-only; otherwise streams
    ``step``/``log``/``launched``/``error`` events. Once ``launched`` is
    received, poll ``/settings/self_update/progress?id=...`` for the swap.
    """
    info = _self_update_info()
    if not info["writable"]:
        return jsonify({"error": "Cannot self-update: " + (info["reason"] or "socket not writable.")}), 400

    data = request.get_json(silent=True) or {}
    image = (data.get("newImage") or "").strip() or None

    from flask import Response, stream_with_context

    def generate():
        for ev in docker_selfupdate.self_update_generator(image):
            event = ev.get("event")
            payload = json.dumps(ev.get("data") or {})
            yield f"event: {event}\ndata: {payload}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@main.route("/settings/self_update/progress", methods=["GET"])
def self_update_progress():
    cid = request.args.get("id")
    if not cid:
        return jsonify({"error": "Container ID is required"}), 400
    return jsonify(docker_selfupdate.poll_updater_progress(cid))

@main.context_processor
def inject_downloaded_models():
    downloads = Download.query.all()
    downloaded_models = {}
    for d in downloads:
        if d.model_id not in downloaded_models:
            downloaded_models[d.model_id] = []
        downloaded_models[d.model_id].append(d.version_id)
        
    return dict(downloaded_models=downloaded_models)

@main.route("/library")
def library():
    type_filter = request.args.get("type")
    query = Download.query
    if type_filter:
        query = query.filter_by(type=type_filter)
        
    models = query.all()
    all_types = db.session.query(Download.type).distinct().all()
    types = [t[0] for t in all_types if t[0]]
    
    return render_template("library.html", models=models, types=types, current_type=type_filter)

@main.route("/files/<path:filename>")
def serve_file(filename):
    if not filename.startswith('/'):
        filename = '/' + filename
    if not os.path.exists(filename):
        return "File not found", 404
    return send_file(filename)

# --------------------------------------------------------------------------- #
# Authentication (local users + OIDC SSO)
# --------------------------------------------------------------------------- #

# Routes that must remain public even when auth is enabled (login page, OIDC
# redirect/callback, static assets, health).
_PUBLIC_ENDPOINTS = {
    "main.auth_login", "main.auth_login_post", "main.auth_logout",
    "main.oidc_initiate", "main.oidc_callback",
    "main.static", "main.serve_file", "main.profile_avatar",
}

@main.before_app_request
def _guard_authentication():
    """When auth is enabled, require a valid session for protected routes.

    Safety valve: if auth is enabled but no users exist yet, the app stays
    open (bootstrap mode) so you can create the first (admin) account without
    locking yourself out.
    """
    if not auth.is_auth_enabled():
        return None
    endpoint = request.endpoint
    if not endpoint or endpoint in _PUBLIC_ENDPOINTS:
        return None
    if not User.query.first():
        return None  # bootstrap: no users yet, keep UI reachable
    # API key is a Civitai credential, not a session — no longer enough.
    from flask import redirect
    if auth.get_session_token_from_request() and auth.current_user():
        return None
    return redirect(url_for("main.auth_login", next=request.path))

def _inject_auth_ctx():
    """Return the auth context dict for this request (user, auth_enabled)."""
    user = None
    try:
        token = auth.get_session_token_from_request()
        user = auth.validate_session(token) if token else None
    except Exception:
        user = None
    return {
        "authenticated": user is not None,
        "user": user,
        "auth_enabled": auth.is_auth_enabled(),
    }

@main.context_processor
def inject_auth_context():
    ctx = _inject_auth_ctx()
    return {"auth_context": ctx}

@main.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    if request.method == "POST":
        return _auth_login_post()
    providers = oidc_mod.enabled_configs()
    return render_template(
        "login.html",
        providers=providers,
        auth_enabled=auth.is_auth_enabled(),
        next_url=request.args.get("next") or "/",
    )

def _auth_login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next") or request.args.get("next") or "/"
    from urllib.parse import urlparse
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"

    client_ip = request.remote_addr or "unknown"
    rate_key = f"{client_ip}:{username}"
    if auth.rate_limited(rate_key):
        flash("Too many login attempts. Please try again later.", "error")
        return render_template(
            "login.html", providers=oidc_mod.enabled_configs(),
            auth_enabled=auth.is_auth_enabled(), next_url=next_url,
        )

    result = auth.authenticate_local(username, password)
    if not result["success"]:
        auth.record_failed(rate_key)
        flash(result.get("error") or "Login failed", "error")
        return render_template(
            "login.html", providers=oidc_mod.enabled_configs(),
            auth_enabled=auth.is_auth_enabled(), next_url=next_url,
        )

    auth.clear_failures(rate_key)
    token = auth.create_session(result["user"].id, provider="local")
    response = redirect(next_url)
    auth.set_session_cookie(response, token)
    return response

@main.route("/auth/logout", methods=["GET", "POST"])
def auth_logout():
    token = auth.get_session_token_from_request()
    auth.destroy_session(token)
    response = redirect(url_for("main.index"))
    auth.clear_session_cookie(response)
    return response

# --------------------------------------------------------------------------- #
# Profile (view / edit email, display name, profile image)
# --------------------------------------------------------------------------- #

_AVATAR_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

@main.route("/profile", methods=["GET", "POST"])
def profile():
    """View / edit the signed-in user's profile (email, display name, avatar)."""
    user = _inject_auth_ctx()["user"]
    if user is None:
        flash("Please sign in to edit your profile.", "warning")
        if auth.is_auth_enabled():
            return redirect(url_for("main.auth_login", next="/profile"))
        return redirect(url_for("main.index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        display_name = (request.form.get("display_name") or "").strip() or user.username
        new_username = (request.form.get("username") or "").strip() or user.username

        # Authenticate the change with the current password (when the account
        # is local and has a password set). OIDC-only accounts skip this check.
        want_delete = request.form.get("remove_avatar") == "1"
        password = request.form.get("password") or ""
        if user.auth_provider.startswith("oidc"):
            ok = True
        elif user.password_hash:
            ok = auth.verify_password(password, user.password_hash)
            if not ok:
                flash("Current password is incorrect.", "error")
                return render_template("profile.html", profile_user=user)
        else:
            ok = True  # no password set yet (bootstrap-created)

        # Username changes are only meaningful for local accounts. OIDC/SSO
        # accounts are matched to their IdP by username on every login, so
        # renaming one would silently create a fresh account on next SSO.
        username_is_local = not user.auth_provider.startswith("oidc")
        if username_is_local and new_username != user.username:
            if len(new_username) < 3:
                flash("Username must be at least 3 characters.", "error")
                return render_template("profile.html", profile_user=user)
            clash = User.query.filter(User.username == new_username, User.id != user.id).first()
            if clash:
                flash(f"Username '{new_username}' is already taken.", "error")
                return render_template("profile.html", profile_user=user)

        if ok:
            if username_is_local:
                user.username = new_username
            user.email = email
            user.display_name = display_name
            avatar_file = request.files.get("avatar")
            if want_delete:
                user.avatar = None
            elif avatar_file and avatar_file.filename:
                ext = avatar_file.filename.rsplit(".", 1)[-1].lower() if "." in avatar_file.filename else ""
                if ext not in _AVATAR_EXTENSIONS:
                    flash("Unsupported image type. Use PNG, JPG, GIF or WebP.", "error")
                    return render_template("profile.html", profile_user=user)
                root = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "static", "uploads", "avatars",
                )
                os.makedirs(root, exist_ok=True)
                name = f"u{user.id}_{uuid.uuid4().hex[:12]}.{ext}"
                avatar_file.save(os.path.join(root, name))
                user.avatar = name
            db.session.commit()
            flash("Profile updated.", "success")
        return redirect(url_for("main.profile"))

    return render_template("profile.html", profile_user=user)


@main.route("/profile/avatar/<int:user_id>")
def profile_avatar(user_id):
    """Serve a user's avatar image by id (public so it can be shown in header
    and rendered in client-side markup)."""
    from werkzeug.utils import safe_join
    user = db.session.get(User, user_id)
    if not user or not user.avatar:
        return "", 404
    root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static", "uploads", "avatars",
    )
    path = safe_join(root, user.avatar)
    if not path or not os.path.exists(path):
        return "", 404
    return send_file(path)


@main.route("/auth/oidc/<int:config_id>/initiate", methods=["GET"])
def oidc_initiate(config_id):
    if not auth.is_auth_enabled():
        flash("Authentication is not enabled.", "warning")
        return redirect(url_for("main.index"))
    config = db.session.get(OidcConfig, config_id)
    if not config or not config.enabled:
        flash("OIDC provider not found or disabled.", "error")
        return redirect(url_for("main.auth_login"))
    next_url = request.args.get("next") or "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    result = oidc_mod.build_authorization_url(config, redirect_url=next_url)
    if result.get("error"):
        flash(f"Could not start SSO: {result['error']}", "error")
        return redirect(url_for("main.auth_login"))
    return redirect(result["url"])

@main.route("/auth/oidc/callback", methods=["GET"])
def oidc_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        flash(f"SSO failed: {error}", "error")
        return redirect(url_for("main.auth_login"))
    if not code or not state:
        flash("Missing OIDC parameters.", "error")
        return redirect(url_for("main.auth_login"))

    result = oidc_mod.handle_callback(code, state)
    if not result["success"]:
        flash(result.get("error") or "SSO authentication failed", "error")
        return redirect(url_for("main.auth_login"))

    token = auth.create_session(result["user"].id, provider=f"oidc:{result['user'].auth_provider}")
    response = redirect(result.get("redirect_url") or "/")
    auth.set_session_cookie(response, token)
    return response

# Settings auth actions (dispatched from the settings form)
def _auth_settings_actions(action):
    """Handle auth-related settings actions; return True if handled.

    Admin-gated: once auth is enabled, only an authenticated admin may manage
    auth settings. During bootstrap (auth disabled) they're open so first-run
    setup is possible.
    """
    # Authorize the manager of the current request.
    def _is_allowed():
        if not auth.is_auth_enabled():
            return True  # bootstrap / no lock yet
        token = auth.get_session_token_from_request()
        user = auth.validate_session(token) if token else None
        return bool(user and user.is_admin)
    if not _is_allowed():
        flash("Admin access required to change auth settings.", "error")
        return True

    if action == "auth:toggle":
        enabled = request.form.get("enabled") == "1"
        auth.set_auth_enabled(enabled)
        flash(f"Authentication {'enabled' if enabled else 'disabled'}.", "success" if enabled else "info")
        return True

    if action == "auth:create_user":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Username and password are required.", "error")
            return True
        if User.query.filter_by(username=username).first():
            flash("User already exists.", "error")
            return True
        user = User(
            username=username, password_hash=auth.hash_password(password),
            is_active=True, is_admin=(request.form.get("is_admin") == "1"),
            auth_provider="local",
        )
        db.session.add(user); db.session.commit()
        flash(f"User '{username}' created.", "success")
        return True

    if action == "auth:update_user":
        try:
            user_id = int(request.form.get("user_id") or "")
        except (TypeError, ValueError):
            user_id = 0
        user = db.session.get(User, user_id)
        if user and not user.auth_provider.startswith("oidc:"):
            pwd = request.form.get("password") or ""
            if pwd:
                user.password_hash = auth.hash_password(pwd)
            if request.form.get("is_admin") == "1":
                user.is_admin = True
            db.session.commit()
            flash(f"User '{user.username}' updated.", "success")
        else:
            flash("User not found or is an SSO account.", "error")
        return True

    if action == "auth:delete_user":
        try:
            user_id = int(request.form.get("user_id") or "")
        except (TypeError, ValueError):
            user_id = 0
        user = db.session.get(User, user_id)
        if user and user.username != "admin":
            db.session.delete(user); db.session.commit()
            flash(f"Deleted user '{user.username}'.", "info")
        else:
            flash("Cannot delete this user.", "error")
        return True

    if action == "auth:create_oidc":
        config = OidcConfig(
            name=(request.form.get("name") or "").strip(),
            enabled=(request.form.get("enabled") == "1"),
            issuer_url=(request.form.get("issuer_url") or "").strip(),
            client_id=(request.form.get("client_id") or "").strip(),
            client_secret=(request.form.get("client_secret") or "").strip(),
            redirect_uri=(request.form.get("redirect_uri") or "").strip(),
            scopes=(request.form.get("scopes") or "openid profile email").strip(),
            username_claim=(request.form.get("username_claim") or "preferred_username").strip(),
            email_claim=(request.form.get("email_claim") or "email").strip(),
            display_name_claim=(request.form.get("display_name_claim") or "name").strip(),
            admin_claim=(request.form.get("admin_claim") or "").strip(),
            admin_value=(request.form.get("admin_value") or "").strip(),
        )
        db.session.add(config); db.session.commit()
        flash(f"OIDC provider '{config.name}' added.", "success")
        return True

    if action == "auth:update_oidc":
        try:
            cid = int(request.form.get("config_id") or "")
        except (TypeError, ValueError):
            cid = 0
        config = db.session.get(OidcConfig, cid)
        if config:
            for field, default in [
                ("name", ""), ("issuer_url", ""), ("client_id", ""), ("redirect_uri", ""),
                ("scopes", "openid profile email"),
                ("username_claim", "preferred_username"), ("email_claim", "email"),
                ("display_name_claim", "name"), ("admin_claim", ""), ("admin_value", ""),
            ]:
                val = (request.form.get(field) or "").strip()
                setattr(config, field, val or default)
            # Only overwrite the client secret if a new one was submitted.
            new_secret = (request.form.get("client_secret") or "").strip()
            if new_secret:
                config.client_secret = new_secret
            config.enabled = (request.form.get("enabled") == "1")
            db.session.commit()
            flash(f"OIDC provider '{config.name}' updated.", "success")
        return True

    if action == "auth:delete_oidc":
        try:
            cid = int(request.form.get("config_id") or "")
        except (TypeError, ValueError):
            cid = 0
        config = db.session.get(OidcConfig, cid)
        if config:
            db.session.delete(config); db.session.commit()
            flash(f"OIDC provider '{config.name}' deleted.", "info")
        return True

    if action == "auth:test_oidc":
        try:
            cid = int(request.form.get("config_id") or "")
        except (TypeError, ValueError):
            cid = 0
        config = db.session.get(OidcConfig, cid)
        if config:
            res = oidc_mod.test_oidc_config(config)
            if res["success"]:
                flash(f"OIDC OK — issuer {res.get('issuer')}", "success")
            else:
                flash(f"OIDC test failed: {res.get('error', '')}", "error")
        return True

    return False
