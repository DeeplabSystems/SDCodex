import os
import sys
import json
import logging
import re
import urllib.request
import urllib.error
import subprocess
import subprocess
import urllib.request
import urllib.error
from jinja2 import ChoiceLoader, FileSystemLoader

logger = logging.getLogger(__name__)

def parse_version(v_str):
    """Parse version string into comparable tuple using only standard library."""
    clean = re.sub(r"[^\d.]", "", str(v_str))
    try:
        return tuple(int(part) for part in clean.split(".") if part.isdigit())
    except Exception:
        return (0,)

# Default official plugin repositories
DEFAULT_PLUGIN_REPOSITORIES = [
    {
        "url": "https://github.com/DeeplabSystems/SDCodex-ComfyCaption",
        "name": "ComfyUI Captioning",
        "description": "Auto-captioning with LLMs/JoyCaption and ComfyUI integration nodes. The gallery now lives in the separate SDCodex Gallery plugin.",
    },
    {
        "url": "https://github.com/DeeplabSystems/SDCodex-Gallery",
        "name": "SDCodex Gallery",
        "description": "Disk-backed media gallery that reads captions, SD prompts and ComfyUI workflows directly from disk and image metadata."
    },
    {
        "url": "https://github.com/DeeplabSystems/SDCodex-GalleryDL",
        "name": "GalleryDL & Tasks",
        "description": "Background gallery-dl and yt-dlp task management, quick downloads, kiosks, OAuth, and a persistent activity log."
    },
    {
        "url": "https://github.com/DeeplabSystems/SDCodex-RemBG",
        "name": "RemBG Background Tools",
        "description": "High-precision background removal, replacement, and batch processing powered by BiRefNet."
    }
]

# Official plugin store. Its plugins.json is the single source of truth for
# which plugins are installable and their latest versions. Add a plugin to that
# JSON (in the SDCodex-Plugin-Store repo) to publish it — no code changes here.
DEFAULT_PLUGIN_STORE_URL = "https://github.com/DeeplabSystems/SDCodex-Plugin-Store"
PLUGIN_STORE_MANIFEST_FILE = "plugins.json"

DEFAULT_PLUGIN_MANIFESTS = {
    "comfy-caption": {
        "id": "comfy-caption",
        "name": "ComfyUI Captioning",
        "version": "2.3.0",
        "description": "Auto-captioning with LLMs/JoyCaption and ComfyUI custom workflow nodes. Captioning GGUF models can be downloaded from HuggingFace via the Caption Models settings page.",
        "author": "DeeplabSystems",
        "repository": "https://github.com/DeeplabSystems/SDCodex-ComfyCaption",
        "entrypoint": "plugin:init_plugin",
        "nav_items": [
            {"label": "Captioning", "url": "/captioning", "icon": "fas fa-tags"}
        ],
        "settings": [
            {"label": "Caption Models", "url": "/caption_models", "icon": "fas fa-headphones-alt"}
        ],
        "volumes": [
            {
                "env_var": "COMFYUI_CUSTOM_NODES",
                "host_path": "./comfyui/custom_nodes",
                "container_path": "/data/custom_nodes",
                "description": "ComfyUI custom-addons folder (where comfyui-sdcodex nodes will be installed)"
            },
            {
                "env_var": "CAPTION_MODELS",
                "host_path": "./caption_models",
                "container_path": "/data/caption_models",
                "description": "Directory where captioning models (GGUF + mmproj) downloaded from the Caption Models page are stored"
            },
            {
                "env_var": "LMSTUDIO_MODELS",
                "host_path": "",
                "container_path": "/data/lmstudio_models",
                "description": "Read-only mount of your LM Studio models directory (point host_path at your LM Studio models folder) so the app can read those GGUF models"
            }
        ]
    },
    "gallery": {
        "id": "gallery",
        "name": "SDCodex Gallery",
        "version": "2.5.9",
        "description": "Disk-backed media gallery built into the SD Codex header. Scans folders directly, reads captions/.txt sidecars, SD prompts and ComfyUI workflows from image metadata, and downloads workflows as JSON. No database required.",
        "author": "DeeplabSystems",
        "repository": "https://github.com/DeeplabSystems/SDCodex-Gallery",
        "entrypoint": "plugin:init_plugin",
        "nav_items": [
            {"label": "Gallery", "url": "/gallery2", "icon": "fas fa-images"}
        ],
        "volumes": [
            {
                "env_var": "GALLERY_ROOT",
                "host_path": "./downloads",
                "container_path": "/data/downloads",
                "description": "Root folder that the gallery browses on disk (media, captions and metadata are read directly from here)."
            }
        ]
    },
    "gallery-dl": {
        "id": "gallery-dl",
        "name": "GalleryDL & Tasks",
        "version": "1.4.0",
        "description": "Background gallery-dl and yt-dlp task management, quick downloads, kiosks, OAuth configuration, and a persistent activity log.",
        "author": "DeeplabSystems",
        "repository": "https://github.com/DeeplabSystems/SDCodex-GalleryDL",
        "entrypoint": "plugin:init_plugin",
        "nav_items": [
            {
                "label": "GalleryDL",
                "icon": "fas fa-download",
                "dropdown": [
                    {"label": "Tasks", "url": "/tasks", "icon": "fas fa-tasks"},
                    {"label": "Quick Download", "url": "/one-time", "icon": "fas fa-download"},
                    {"label": "Kiosks", "url": "/kiosks", "icon": "fas fa-desktop"},
                    {"label": "OAuth", "url": "/oauth", "icon": "fas fa-key"},
                    {"label": "Logs", "url": "/logs", "icon": "fas fa-list-alt"},
                    {"label": "GDL Config", "url": "/config", "icon": "fas fa-sliders-h"}
                ]
            }
        ],
        "settings": [
            {"label": "GDL Config", "url": "/config?tab=config", "icon": "fas fa-cog"},
            {"label": "GDL Task Scheduler", "url": "/config?tab=scheduler", "icon": "fas fa-calendar-alt"},
            {"label": "GDL Task Backup & Restore", "url": "/config?tab=backup", "icon": "fas fa-history"}
        ],
        "volumes": [
            {
                "env_var": "TASKS",
                "host_path": "./tasks",
                "container_path": "/data/tasks",
                "description": "Directory for background task definitions, outputs and logs"
            },
            {
                "env_var": "CONFIG",
                "host_path": "./config",
                "container_path": "/data/config",
                "description": "Directory for gallery-dl.conf, kiosk configurations, and secrets"
            },
            {
                "env_var": "IMAGES",
                "host_path": "./downloads",
                "container_path": "/data/downloads",
                "description": "Output directory for quick downloads and tools"
            }
        ]
    },
    "rembg": {
        "id": "rembg",
        "name": "RemBG Background Tools",
        "version": "1.1.0",
        "description": "High-precision background removal, replacement, and batch processing powered by BiRefNet.",
        "author": "DeeplabSystems",
        "repository": "https://github.com/DeeplabSystems/SDCodex-RemBG",
        "entrypoint": "plugin:init_plugin",
        "nav_items": [
            {
                "label": "RemBG",
                "icon": "fas fa-eraser",
                "dropdown": [
                    {"label": "BG Remove", "url": "/rembg", "icon": "fas fa-eraser"},
                    {"label": "BG Replace", "url": "/rembg/replace", "icon": "fas fa-layer-group"},
                    {"label": "BG Batch", "url": "/rembg/batch", "icon": "fas fa-images"}
                ]
            }
        ],
        "volumes": [
            {
                "env_var": "HFH",
                "host_path": "./Huggingface",
                "container_path": "/data/huggingface",
                "description": "HuggingFace model cache directory for BiRefNet weights"
            },
            {
                "env_var": "REMBG_OUTPUT",
                "host_path": "./rembg_output",
                "container_path": "/data/rembg_output",
                "description": "Directory where processed images without backgrounds are stored"
            }
        ]
    }
}

class PluginManager:
    def __init__(self):
        self.app = None
        self.db = None
        self.plugins_dir = None
        self.loaded_plugins = {}
        self.update_cache = {}
        self.store_info = {
            "store": {"name": "SDCodex Plugin Store", "url": DEFAULT_PLUGIN_STORE_URL, "reachable": False},
            "plugins": [],
            "new_plugins": [],
            "last_checked": None,
        }

    def init_app(self, app, db):
        self.app = app
        self.db = db
        
        # Determine plugins directory
        # Priority: PLUGINS_DIR env var -> <root>/plugins -> /app/plugins
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.plugins_dir = os.getenv("PLUGINS_DIR")
        if not self.plugins_dir:
            self.plugins_dir = os.path.join(root_dir, "plugins")
            
        os.makedirs(self.plugins_dir, exist_ok=True)
        
        # Initialize default repositories in DB if needed
        self._ensure_default_repos()
        
        # Discover and load installed plugins
        self.load_plugins()
        
        # Register Jinja context processors for navbar and updates
        @app.context_processor
        def inject_plugin_context():
            return {
                "plugin_nav_items": self.get_nav_items(),
                "plugin_updates_count": self.get_pending_updates_count(),
                "installed_plugins": self.get_installed_plugins()
            }

    def _ensure_default_repos(self):
        """Seed default plugin repositories into the database if not present."""
        try:
            with self.app.app_context():
                from app.models import PluginRepo
                existing_urls = {r.repo_url.rstrip("/").lower() for r in PluginRepo.query.all()}
                for repo in DEFAULT_PLUGIN_REPOSITORIES:
                    clean_url = repo["url"].rstrip("/").lower()
                    if clean_url not in existing_urls:
                        new_repo = PluginRepo(
                            repo_url=repo["url"],
                            name=repo["name"],
                            description=repo["description"]
                        )
                        self.db.session.add(new_repo)
                self.db.session.commit()
        except Exception as e:
            logger.warning("Could not seed default plugin repositories: %s", e)

    def load_plugins(self):
        """Scans plugins_dir, reads manifests, and registers active plugins."""
        if not os.path.exists(self.plugins_dir):
            return

        extra_template_dirs = []

        for item in sorted(os.listdir(self.plugins_dir)):
            plugin_path = os.path.join(self.plugins_dir, item)
            if not os.path.isdir(plugin_path):
                continue
                
            manifest_file = os.path.join(plugin_path, "plugin.json")
            if not os.path.exists(manifest_file):
                continue

            disabled_file = os.path.join(plugin_path, ".disabled")
            is_enabled = not os.path.exists(disabled_file)

            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                    
                plugin_id = manifest.get("id", item)
                manifest["_path"] = plugin_path
                manifest["_enabled"] = is_enabled
                
                # Ensure container environment variables are set for plugin volumes
                for v in manifest.get("volumes", []):
                    env_var = v.get("env_var")
                    c_path = v.get("container_path")
                    if env_var and c_path:
                        os.environ.setdefault(env_var, c_path)

                if not is_enabled:
                    self.loaded_plugins[plugin_id] = manifest
                    continue

                # Add plugin directory to sys.path so its modules can be imported
                if plugin_path not in sys.path:
                    sys.path.insert(0, plugin_path)

                # If plugin has templates, add to Jinja template loaders
                plugin_templates = os.path.join(plugin_path, "templates")
                if os.path.isdir(plugin_templates):
                    extra_template_dirs.append(plugin_templates)

                # Initialize plugin via entrypoint
                entrypoint = manifest.get("entrypoint", "plugin:init_plugin")
                module_name, func_name = entrypoint.split(":", 1)
                
                # Import module dynamically using isolated module namespace per plugin
                import importlib.util
                py_file = os.path.join(plugin_path, f"{module_name}.py")
                if not os.path.exists(py_file):
                    logger.warning(f"Plugin entrypoint file not found: {py_file}")
                    continue

                module_unique_name = f"sdcodex_plugin_{plugin_id.replace('-', '_')}"
                spec = importlib.util.spec_from_file_location(module_unique_name, py_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[module_unique_name] = mod
                    spec.loader.exec_module(mod)
                    init_fn = getattr(mod, func_name)
                    init_fn(self.app, self.db, manifest)
                    self.loaded_plugins[plugin_id] = manifest
                    logger.info(f"Loaded plugin: {manifest.get('name', plugin_id)} ({plugin_id})")
                else:
                    logger.warning(f"Could not create spec for plugin {plugin_id}")

            except Exception as e:
                logger.warning(f"Plugin {item} skipped or failed to load: {e}")
                manifest["_load_error"] = str(e)
                self.loaded_plugins[plugin_id] = manifest

        # Update Jinja loader so templates from plugins can be rendered transparently
        if extra_template_dirs and self.app:
            current_loader = self.app.jinja_loader
            all_loaders = [current_loader] + [FileSystemLoader(d) for d in extra_template_dirs]
            self.app.jinja_loader = ChoiceLoader(all_loaders)

    def get_nav_items(self):
        """Aggregate navigation items from all loaded and enabled plugins."""
        nav_items = []
        for plugin_id, manifest in self.loaded_plugins.items():
            if not manifest.get("_enabled", False):
                continue
            items = manifest.get("nav_items", [])
            for item in items:
                nav_items.append(item)
        return nav_items

    def get_installed_plugins(self):
        """Return list of all installed plugins with status and update info."""
        plugins = []
        for plugin_id, manifest in self.loaded_plugins.items():
            p = dict(manifest)
            p["has_update"] = self.update_cache.get(plugin_id, {}).get("has_update", False)
            p["latest_version"] = self.update_cache.get(plugin_id, {}).get("latest_version", manifest.get("version"))
            plugins.append(p)
        return plugins

    def get_pending_updates_count(self):
        """Count how many installed plugins have an update available."""
        count = 0
        for plugin_id in self.loaded_plugins:
            if self.update_cache.get(plugin_id, {}).get("has_update", False):
                count += 1
        return count

    def normalize_github_url(self, url):
        """Normalize URL or owner/repo to owner/repo and canonical URL."""
        url = url.strip()
        # Clean git@github.com:owner/repo.git or https://github.com/owner/repo.git
        if url.startswith("git@github.com:"):
            url = url.replace("git@github.com:", "https://github.com/")
        if url.endswith(".git"):
            url = url[:-4]
            
        m = re.search(r"github\.com/([^/]+)/([^/]+)", url)
        if m:
            owner, repo = m.group(1), m.group(2)
            return f"{owner}/{repo}", f"https://github.com/{owner}/{repo}"
        elif "/" in url and not url.startswith("http"):
            parts = url.split("/")
            return f"{parts[0]}/{parts[1]}", f"https://github.com/{parts[0]}/{parts[1]}"
            
        return url, url

    def _get_github_token(self):
        token = os.environ.get("GITHUB_TOKEN")
        if token and token.strip():
            return token.strip()
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
                                if k.strip() == "GITHUB_TOKEN" and v.strip():
                                    return v.strip()
                except Exception:
                    pass
        return None

    def get_default_manifest(self, identifier):
        """Lookup default manifest for known official plugins."""
        if not identifier:
            return None
        ident = identifier.lower().strip()
        for k, m in DEFAULT_PLUGIN_MANIFESTS.items():
            if k == ident or m.get("id") == ident or ident.endswith(k) or k in ident or ident in m.get("repository", "").lower():
                import copy
                return copy.deepcopy(m)
        return None

    def fetch_remote_manifest(self, repo_url):
        """Fetch plugin.json from GitHub, local workspace, or known defaults fallback."""
        short_name, full_url = self.normalize_github_url(repo_url)
        repo_name = short_name.split("/")[-1] if "/" in short_name else short_name
        
        # Check local sibling / dev directories first
        local_candidates = [
            os.environ.get("LOCAL_PLUGINS_DIR"),
            os.path.join(os.path.dirname(self.plugins_dir), "..", repo_name),
            os.path.join("/workspace", repo_name),
            os.path.join("/plugins_dev", repo_name),
            os.path.join("/home/naked/workspace/deeplabs", repo_name),
            os.path.join("/home/naked/dev", repo_name)
        ]
        for candidate in local_candidates:
            if not candidate:
                continue
            cand_manifest = os.path.join(candidate, "plugin.json")
            if os.path.exists(cand_manifest):
                try:
                    with open(cand_manifest, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        data["_source_type"] = "local"
                        data["_local_path"] = os.path.abspath(candidate)
                        return data
                except Exception:
                    pass

        # Try fetching from GitHub (supporting authenticated requests for private repos)
        token = self._get_github_token()
        headers = {"User-Agent": "SDCodex-Plugin-Manager/1.0"}
        if token:
            headers["Authorization"] = f"token {token}"

        # 1. Try raw.githubusercontent.com
        for branch in ["main", "master"]:
            raw_url = f"https://raw.githubusercontent.com/{short_name}/{branch}/plugin.json"
            try:
                req = urllib.request.Request(raw_url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    data["_source_type"] = "github"
                    return data
            except Exception:
                continue

        # 2. Try GitHub API contents endpoint (if token available or raw returned 404)
        api_url = f"https://api.github.com/repos/{short_name}/contents/plugin.json"
        api_headers = dict(headers)
        api_headers["Accept"] = "application/vnd.github.v3.raw"
        try:
            req = urllib.request.Request(api_url, headers=api_headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                data["_source_type"] = "github"
                return data
        except Exception:
            pass

        # 3. Fallback: Return known default manifest with volume definitions if available
        default_manifest = self.get_default_manifest(short_name)
        if default_manifest:
            default_manifest["_source_type"] = "default"
            return default_manifest

        return None

    def get_store_url(self):
        """Resolve the plugin store URL (env override or default official store)."""
        return (os.environ.get("PLUGIN_STORE_URL") or DEFAULT_PLUGIN_STORE_URL).strip().rstrip("/")

    def fetch_store_manifest(self):
        """Fetch and parse the plugin store's plugins.json manifest.

        Reads from the local store clone first (dev/offline), then from GitHub,
        using the caller-supplied or env GITHUB_TOKEN for private stores.
        Returns the parsed JSON (with a raw URL of the store, plus resolved local
        path when available) or None on failure.
        """
        store_url = self.get_store_url()
        short_name, _ = self.normalize_github_url(store_url)
        store_repo_name = short_name.split("/")[-1] if "/" in short_name else short_name

        # 1. Local store clone / dev directories first
        local_candidates = [
            os.environ.get("LOCAL_PLUGIN_STORE_DIR"),
            os.environ.get("LOCAL_PLUGINS_DIR"),
            os.path.join(os.path.dirname(self.plugins_dir), "..", store_repo_name),
            os.path.join("/workspace", store_repo_name),
            os.path.join("/plugins_dev", store_repo_name),
            os.path.join("/home/naked/workspace/deeplabs", store_repo_name),
            os.path.join("/home/naked/dev", store_repo_name),
        ]
        for candidate in local_candidates:
            if not candidate:
                continue
            cand_file = os.path.join(candidate, PLUGIN_STORE_MANIFEST_FILE)
            if os.path.exists(cand_file):
                try:
                    with open(cand_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        data["_source_type"] = "local"
                        data["_local_path"] = os.path.abspath(candidate)
                        data["_store_url"] = store_url
                        return data
                except Exception:
                    pass

        # 2. Fetch from GitHub (auth for private stores)
        token = self._get_github_token()
        headers = {"User-Agent": "SDCodex-Plugin-Manager/1.0"}
        if token:
            headers["Authorization"] = f"token {token}"

        for branch in ["main", "master"]:
            raw_url = f"https://raw.githubusercontent.com/{short_name}/{branch}/{PLUGIN_STORE_MANIFEST_FILE}"
            try:
                req = urllib.request.Request(raw_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    data["_source_type"] = "github"
                    data["_store_url"] = store_url
                    return data
            except Exception:
                continue

        # 3. GitHub API fallback
        try:
            api_url = f"https://api.github.com/repos/{short_name}/contents/{PLUGIN_STORE_MANIFEST_FILE}"
            api_headers = {"User-Agent": "SDCodex-Plugin-Manager/1.0", "Accept": "application/vnd.github.v3.raw"}
            if token:
                api_headers["Authorization"] = f"token {token}"
            req = urllib.request.Request(api_url, headers=api_headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                data["_source_type"] = "github"
                data["_store_url"] = store_url
                return data
        except Exception:
            pass

        return None

    def get_store_catalog(self):
        """Return the store manifest with a normalized dict of plugin entries.

        Result:
          { "store": <store manifest metadata + url/path>,
            "plugins": [ <entry>, ... ] }
          The 'plugins' list is the raw entries from plugins.json. If the store
          cannot be reached, falls back to the default plugin list.
        """
        data = self.fetch_store_manifest()
        if not data or not isinstance(data.get("plugins"), list):
            # Fallback to default repositories for resilience/offline use.
            plugins = []
            for repo in DEFAULT_PLUGIN_REPOSITORIES:
                m = self.get_default_manifest(repo["url"].rstrip("/").split("/")[-1])
                if m is None:
                    m = {
                        "id": repo["url"].rstrip("/").split("/")[-1].lower(),
                        "name": repo["name"],
                        "description": repo["description"],
                        "repository": repo["url"],
                        "version": "1.0.0",
                    }
                plugins.append(m)
            return {
                "store": {
                    "name": "SDCodex Plugin Store",
                    "description": "Official store of SDCodex community plugins.",
                    "url": self.get_store_url(),
                    "reachable": False,
                },
                "plugins": plugins,
            }

        metadata = {
            "name": data.get("name", "SDCodex Plugin Store"),
            "description": data.get("description", ""),
            "version": data.get("version", ""),
            "url": data.get("_store_url") or self.get_store_url(),
            "source_type": data.get("_source_type", ""),
            "local_path": data.get("_local_path", ""),
            "reachable": True,
        }
        plugins = data.get("plugins", [])
        return {"store": metadata, "plugins": plugins}

    def check_updates(self):
        """Check the plugin store for updated versions and new plugins.

        Reads plugins.json from the store, fetches each plugin's authoritative
        manifest (from its repository), and builds:
          - update_cache: per-installed-plugin version status (drives the
            per-card 'Update available' and the navbar badge).
          - store_info.new_plugins: store plugins not yet installed.
        """
        from datetime import datetime

        catalog = self.get_store_catalog()
        store_meta = catalog["store"]
        store_plugins = catalog["plugins"]

        results = {}
        new_plugins = []

        # Build a lookup of store entries by normalized plugin id/repo sl.
        store_by_id = {}
        for entry in store_plugins:
            eid = entry.get("id", "")
            key = eid.lower()
            if key:
                store_by_id.setdefault(key, entry)

        for plugin_id, manifest in self.loaded_plugins.items():
            repo_url = manifest.get("repository")
            if not repo_url:
                continue
            remote_manifest = self.fetch_remote_manifest(repo_url)
            if not remote_manifest:
                continue
            current_ver = str(manifest.get("version", "0.0.0"))
            remote_ver = str(remote_manifest.get("version", current_ver))
            try:
                has_update = parse_version(remote_ver) > parse_version(current_ver)
            except Exception:
                has_update = (remote_ver != current_ver)
            results[plugin_id] = {
                "has_update": has_update,
                "current_version": current_ver,
                "latest_version": remote_ver,
                "repository": repo_url,
                "remote_manifest": remote_manifest,
            }

        # New plugins = store catalog entries not currently installed.
        for entry in store_plugins:
            eid = entry.get("id", "")
            if not eid:
                continue
            if eid in self.loaded_plugins:
                continue
            if not any(eid == pid or eid.lower() == pid.lower() for pid in self.loaded_plugins):
                # Resolve the plugin's latest version from its own manifest if possible
                repo_url = entry.get("repository")
                latest = entry.get("version", "")
                if repo_url:
                    remote_manifest = self.fetch_remote_manifest(repo_url)
                    if remote_manifest:
                        latest = str(remote_manifest.get("version", latest or "1.0.0"))
                item = dict(entry)
                item["latest_version"] = latest or "1.0.0"
                new_plugins.append(item)

        self.update_cache = results
        self.store_info = {
            "store": store_meta,
            "plugins": store_plugins,
            "new_plugins": new_plugins,
            "last_checked": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        return {"update_cache": results, "new_plugins": new_plugins, "store": store_meta}

    def get_available_uninstalled_plugins(self):
        """Get catalog of plugins that are not yet installed.

        Primary source is the plugin store's plugins.json. Offline/unreachable
        stores fall back to the default plugin list / registered repositories so
        the catalog is never empty in normal use.
        """
        available = []
        seen_ids = {pid.lower() for pid in self.loaded_plugins}

        catalog = self.get_store_catalog()
        for entry in catalog["plugins"]:
            pid = entry.get("id", "")
            if not pid or pid.lower() in seen_ids:
                continue
            repo_url = entry.get("repository")
            manifest = None
            if repo_url:
                manifest = self.fetch_remote_manifest(repo_url)
            if not manifest:
                manifest = dict(entry)
                manifest.setdefault("volumes", [])
                manifest.setdefault("version", "1.0.0")
                manifest.setdefault("author", "")
            manifest.setdefault("_repo_id", None)
            manifest["_repo_url"] = repo_url or entry.get("url", "")
            manifest["_store"] = True
            available.append(manifest)

        # Backward-compat fallback: if the store returned no catalog (e.g. a
        # legacy/empty store), fall back to registered PluginRepo rows and the
        # default plugin list so nothing disappears.
        if not catalog["plugins"] or (available and not any(p.get("_store") for p in available)):
            available = []
            seen_ids = {pid.lower() for pid in self.loaded_plugins}
            try:
                with self.app.app_context():
                    from app.models import PluginRepo
                    repos = PluginRepo.query.all()
            except Exception:
                repos = []
            for repo in repos:
                manifest = self.fetch_remote_manifest(repo.repo_url)
                if manifest:
                    pid = manifest.get("id")
                    if pid and pid.lower() not in seen_ids:
                        manifest["_repo_id"] = repo.id
                        manifest["_repo_url"] = repo.repo_url
                        available.append(manifest)
                else:
                    short_name, full_url = self.normalize_github_url(repo.repo_url)
                    repo_slug = short_name.split("/")[-1].lower()
                    default_m = self.get_default_manifest(repo_slug)
                    if default_m:
                        pid = default_m.get("id", repo_slug)
                        if pid.lower() not in seen_ids:
                            default_m["_repo_id"] = repo.id
                            default_m["_repo_url"] = repo.repo_url
                            available.append(default_m)
                    elif not any(repo_slug in pid2 for pid2 in self.loaded_plugins):
                        available.append({
                            "id": repo_slug,
                            "name": repo.name or repo_slug,
                            "description": repo.description or "Plugin repository from GitHub",
                            "version": "1.0.0",
                            "repository": full_url,
                            "volumes": [],
                            "_repo_id": repo.id,
                            "_repo_url": repo.repo_url
                        })
        return available

    def install_plugin(self, repo_url, volume_paths=None):
        """Clones/copies plugin to plugins_dir, configures .env, requirements.txt, and docker-compose.override.yml."""
        short_name, full_url = self.normalize_github_url(repo_url)
        manifest = self.fetch_remote_manifest(repo_url)
        repo_name = short_name.split("/")[-1] if "/" in short_name else short_name
        
        plugin_id = manifest.get("id") if manifest else repo_name.lower()
        target_dir = os.path.join(self.plugins_dir, plugin_id)

        # 1. Install Code
        clone_error = ""
        token = self._get_github_token()

        if manifest and manifest.get("_source_type") == "local" and os.path.exists(manifest.get("_local_path", "")):
            # Local copy / link for development
            import shutil
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(manifest["_local_path"], target_dir, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        else:
            # Git clone (using auth token if available)
            clone_url = full_url
            if token and "github.com" in full_url:
                clone_url = f"https://x-access-token:{token}@github.com/{short_name}.git"

            git_env = dict(os.environ)
            git_env["GIT_TERMINAL_PROMPT"] = "0"

            if os.path.exists(target_dir) and os.path.exists(os.path.join(target_dir, ".git")):
                cmd = ["git", "-C", target_dir, "pull", "origin", "main"]
                res = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
                if res.returncode != 0:
                    clone_error = res.stderr.strip()
            else:
                cmd = ["git", "clone", clone_url, target_dir]
                res = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
                if res.returncode != 0:
                    # Try master branch
                    cmd = ["git", "clone", "-b", "master", clone_url, target_dir]
                    res = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
                    if res.returncode != 0:
                        clone_error = res.stderr.strip()

            # If git clone failed, try checking local candidates fallback
            if not os.path.exists(target_dir) or not os.listdir(target_dir):
                root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                local_candidates = [
                    os.environ.get("LOCAL_PLUGINS_DIR"),
                    os.path.join(os.path.dirname(self.plugins_dir), "..", repo_name),
                    os.path.join(os.path.dirname(root_dir), repo_name),
                    os.path.join("/workspace", repo_name),
                    os.path.join("/plugins_dev", repo_name),
                    os.path.join("/home/naked/workspace/deeplabs", repo_name),
                    os.path.join("/home/naked/dev", repo_name)
                ]
                for candidate in local_candidates:
                    if candidate and os.path.exists(candidate) and os.path.exists(os.path.join(candidate, "plugin.json")):
                        import shutil
                        if os.path.exists(target_dir):
                            shutil.rmtree(target_dir)
                        shutil.copytree(candidate, target_dir, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                        logger.info(f"Copied plugin from local candidate: {candidate}")
                        clone_error = ""
                        break

        # Verification: Ensure plugin files actually exist
        if not os.path.exists(target_dir) or not os.listdir(target_dir):
            error_msg = f"Failed to download plugin code for '{plugin_id}'."
            if clone_error:
                error_msg += f" Git output: {clone_error}."
            if not token:
                error_msg += " This repository may be private. Please configure your GITHUB_TOKEN in Settings -> Plugins or your .env file."
            return False, error_msg

        # 1b. Update root requirements.txt and install requirements in background
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        req_path = os.path.join(target_dir, "requirements.txt")
        if os.path.exists(req_path):
            self._update_requirements_file(root_dir, plugin_id, req_path)

            # Asynchronous background pip installation to prevent blocking web request / gunicorn worker
            def _bg_install(r_path, p_id):
                try:
                    logger.info(f"Starting background pip install for plugin '{p_id}'...")
                    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", r_path]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                    if res.returncode == 0:
                        logger.info(f"Background pip install completed successfully for '{p_id}'.")
                        self.load_plugins()
                    else:
                        logger.warning(f"Background pip install for '{p_id}' returned non-zero: {res.stderr}")
                except Exception as pip_err:
                    logger.warning(f"Error during background pip install for '{p_id}': {pip_err}")

            import threading
            threading.Thread(target=_bg_install, args=(req_path, plugin_id), daemon=True).start()

        # 2. Re-read manifest from installed directory
        installed_manifest_path = os.path.join(target_dir, "plugin.json")
        if os.path.exists(installed_manifest_path):
            with open(installed_manifest_path, "r", encoding="utf-8") as f:
                installed_manifest = json.load(f)
        else:
            installed_manifest = manifest or self.get_default_manifest(plugin_id) or {"id": plugin_id, "name": plugin_id, "volumes": []}

        # 3. Configure Volumes in .env and docker-compose.override.yml
        if volume_paths is None:
            volume_paths = {}
        for v in installed_manifest.get("volumes", []):
            env_var = v.get("env_var")
            default_host = v.get("host_path", "")
            if env_var and (env_var not in volume_paths or not volume_paths[env_var]):
                volume_paths[env_var] = default_host

        self.apply_volume_config(installed_manifest, volume_paths)

        # 4. Reload plugins in runtime
        self.load_plugins()
        return True, f"Plugin '{installed_manifest.get('name', plugin_id)}' installed successfully!"

    def update_plugin(self, plugin_id):
        """Updates plugin repository to latest commit."""
        plugin_dir = os.path.join(self.plugins_dir, plugin_id)
        if not os.path.exists(plugin_dir):
            return False, "Plugin directory not found."

        # If it's a git clone, do git pull
        if os.path.exists(os.path.join(plugin_dir, ".git")):
            git_env = dict(os.environ)
            git_env["GIT_TERMINAL_PROMPT"] = "0"
            res = subprocess.run(["git", "-C", plugin_dir, "pull"], capture_output=True, text=True, env=git_env)
            if res.returncode != 0:
                return False, f"Git pull failed: {res.stderr}"
        else:
            # Check if there is a local sibling or re-fetch
            manifest_file = os.path.join(plugin_dir, "plugin.json")
            if os.path.exists(manifest_file):
                try:
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    repo_url = manifest.get("repository")
                    if repo_url:
                        return self.install_plugin(repo_url)
                except Exception:
                    pass

        # Clear update cache for this plugin
        if plugin_id in self.update_cache:
            del self.update_cache[plugin_id]

        self.load_plugins()
        return True, f"Plugin '{plugin_id}' updated successfully!"

    def uninstall_plugin(self, plugin_id):
        """Removes plugin directory, volume entries, env vars, and requirements."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plugin_dir = os.path.join(self.plugins_dir, plugin_id)

        # Remove volumes + env from docker-compose.override.yml
        self.remove_volume_config_from_compose(plugin_id)

        # Remove requirements from root requirements.txt
        self._remove_requirements_from_file(root_dir, plugin_id)

        # Remove plugin env vars from .env (the env vars this plugin introduced)
        self._remove_env_keys_for_plugin(plugin_id)

        # Remove files
        if os.path.exists(plugin_dir):
            import shutil
            shutil.rmtree(plugin_dir)

        if plugin_id in self.loaded_plugins:
            del self.loaded_plugins[plugin_id]
        if plugin_id in self.update_cache:
            del self.update_cache[plugin_id]

        return True, f"Plugin '{plugin_id}' uninstalled successfully."

    def toggle_plugin(self, plugin_id, enabled):
        """Enable or disable a plugin."""
        plugin_dir = os.path.join(self.plugins_dir, plugin_id)
        disabled_file = os.path.join(plugin_dir, ".disabled")
        if enabled:
            if os.path.exists(disabled_file):
                os.remove(disabled_file)
        else:
            with open(disabled_file, "w") as f:
                f.write("disabled")
                
        self.load_plugins()
        return True

    def _update_requirements_file(self, root_dir, plugin_id, plugin_req_path):
        """Appends new requirements from plugin to root requirements.txt if not already present."""
        if not os.path.exists(plugin_req_path):
            return

        req_file = os.path.join(root_dir, "requirements.txt")
        existing_lines = []
        if os.path.exists(req_file) and not os.path.isdir(req_file):
            try:
                with open(req_file, "r", encoding="utf-8") as f:
                    existing_lines = f.readlines()
            except Exception as e:
                logger.error(f"Error reading root requirements.txt: {e}")

        # Extract normalized existing package names
        existing_pkgs = set()
        for line in existing_lines:
            line_str = line.strip()
            if line_str and not line_str.startswith("#"):
                pkg_name = re.split(r"[=<>~!]", line_str)[0].strip().lower()
                existing_pkgs.add(pkg_name)

        # Read plugin requirements
        try:
            with open(plugin_req_path, "r", encoding="utf-8") as f:
                plugin_lines = f.readlines()
        except Exception as e:
            logger.error(f"Error reading plugin requirements.txt: {e}")
            return

        new_reqs = []
        for line in plugin_lines:
            line_str = line.strip()
            if line_str and not line_str.startswith("#"):
                pkg_name = re.split(r"[=<>~!]", line_str)[0].strip().lower()
                if pkg_name not in existing_pkgs:
                    new_reqs.append(line_str)
                    existing_pkgs.add(pkg_name)

        if not new_reqs:
            return

        content = "".join(existing_lines)
        if content and not content.endswith("\n"):
            content += "\n"

        plugin_block = f"\n# [{plugin_id}]\n" + "\n".join(new_reqs) + "\n"
        content += plugin_block

        try:
            with open(req_file, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Updated requirements.txt with {len(new_reqs)} new package(s) for '{plugin_id}'.")
        except Exception as e:
            logger.error(f"Error writing requirements.txt: {e}")

    def _remove_requirements_from_file(self, root_dir, plugin_id):
        """Removes the plugin's requirement section from root requirements.txt."""
        req_file = os.path.join(root_dir, "requirements.txt")
        if not os.path.exists(req_file) or os.path.isdir(req_file):
            return

        try:
            with open(req_file, "r", encoding="utf-8") as f:
                content = f.read()

            pattern = re.compile(
                rf"\n?#\s*\[{re.escape(plugin_id)}\].*?(?=(\n#\s*\[|\Z))",
                re.DOTALL
            )
            new_content = pattern.sub("", content)

            with open(req_file, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            logger.error(f"Error removing requirements for '{plugin_id}': {e}")

    def _remove_env_keys_for_plugin(self, plugin_id):
        """Removes the .env keys that this plugin introduced (from its manifest volumes)."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        manifest = self.loaded_plugins.get(plugin_id, {})
        volume_env_vars = {v.get("env_var") for v in manifest.get("volumes", []) if v.get("env_var")}
        if not volume_env_vars:
            # Fall back to the default manifest so uninstall works even at runtime before load
            default = self.get_default_manifest(plugin_id)
            if default:
                volume_env_vars = {v.get("env_var") for v in default.get("volumes", []) if v.get("env_var")}
        if not volume_env_vars:
            return

        env_file = os.path.join(root_dir, ".env")
        if not os.path.exists(env_file) or os.path.isdir(env_file):
            return

        try:
            with open(env_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept = [ln for ln in lines if not (ln.strip() and not ln.startswith("#") and "=" in ln and ln.split("=", 1)[0].strip() in volume_env_vars)]
            if len(kept) != len(lines):
                with open(env_file, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                logger.info(f"Removed env vars {sorted(volume_env_vars)} from .env for '{plugin_id}'")
        except Exception as e:
            logger.error(f"Error removing env vars for '{plugin_id}': {e}")

    def get_plugin_compose_file(self):
        """Path of the compose file that holds per-plugin mounts.

        Plugin volume/environment entries live in docker-compose.override.yml so
        the core docker-compose.yml is never modified at runtime. Docker Compose
        auto-merges the override file with docker-compose.yml on `up`.
        """
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root_dir, "docker-compose.override.yml")

    def apply_volume_config(self, manifest, volume_values):
        """Updates .env and docker-compose.override.yml with volume paths for this plugin."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_file = os.path.join(root_dir, ".env")
        compose_file = self.get_plugin_compose_file()

        # 1. Update .env file
        env_updates = {}
        volumes = manifest.get("volumes", [])
        for v in volumes:
            env_var = v.get("env_var")
            val = volume_values.get(env_var, v.get("host_path", "")).strip()
            if env_var and val:
                env_updates[env_var] = val

        self._update_env_file(env_file, env_updates)

        # 2. Update docker-compose.override.yml (volume mounts and environment block)
        plugin_id = manifest.get("id")
        self._update_compose_volumes(compose_file, plugin_id, volumes)
        self._update_compose_environment(compose_file, plugin_id, volumes)

    def _update_env_file(self, env_path, updates):
        """Safely updates .env preserving existing keys, comments, and structure."""
        if not updates:
            return

        # Guard against directory mount
        if os.path.isdir(env_path):
            logger.error(f"Cannot update {env_path}: path is a directory (Docker empty-dir mount).")
            # Try alternate file 'env' if present
            alt = os.path.join(os.path.dirname(env_path), "env")
            if os.path.exists(alt) and not os.path.isdir(alt):
                env_path = alt
            else:
                return

        root_dir = os.path.dirname(os.path.abspath(env_path))
        lines = []

        if os.path.exists(env_path) and not os.path.isdir(env_path) and os.path.getsize(env_path) > 0:
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception as e:
                logger.error(f"Could not read {env_path}: {e}")

        # If .env was missing or empty, seed from 'env' or 'env.example'
        if not lines:
            alt_env = os.path.join(root_dir, "env")
            example_env = os.path.join(root_dir, "env.example")
            seed_path = None
            if os.path.exists(alt_env) and not os.path.isdir(alt_env) and os.path.getsize(alt_env) > 0:
                seed_path = alt_env
            elif os.path.exists(example_env) and not os.path.isdir(example_env):
                seed_path = example_env

            if seed_path:
                try:
                    with open(seed_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    logger.info(f"Seeded .env structure from {os.path.basename(seed_path)}")
                except Exception as e:
                    logger.warning(f"Could not seed .env from {seed_path}: {e}")

        existing_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _ = stripped.split("=", 1)
                k = k.strip()
                if k in updates:
                    new_lines.append(f"{k}={updates[k]}\n")
                    existing_keys.add(k)
                    continue
                else:
                    existing_keys.add(k)
            new_lines.append(line)

        # Append remaining new keys
        remaining_keys = [k for k in updates if k not in existing_keys]
        if remaining_keys:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            for k in remaining_keys:
                new_lines.append(f"{k}={updates[k]}\n")

        # Safety check: Never write an empty file
        if not new_lines:
            return

        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            logger.info(f"Successfully updated {env_path} with keys: {list(updates.keys())}")
        except Exception as e:
            logger.error(f"Failed to write updates to {env_path}: {e}")

    def _update_compose_volumes(self, compose_path, plugin_id, volumes):
        if not os.path.exists(compose_path) or not volumes:
            return

        try:
            with open(compose_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Could not read compose file {compose_path}: {e}")
            return

        start_marker = "# --- PLUGIN VOLUMES START ---"
        end_marker = "# --- PLUGIN VOLUMES END ---"

        if start_marker not in content or end_marker not in content:
            # If markers are missing, find where volumes block is
            match = re.search(r"(\s+volumes:\s*\n)", content)
            if match:
                indent = "      "
                # Check if there are already volume entries under volumes:
                # Find end of volumes list or insert after volumes:
                cfg_match = re.search(r"(\s+#\s*---\s*PLUGIN CONFIG MOUNTS.*?\n(?:\s+-\s+.*?\n)+)", content)
                if cfg_match:
                    idx = cfg_match.end()
                    insertion = f"{indent}{start_marker}\n{indent}{end_marker}\n"
                    content = content[:idx] + insertion + content[idx:]
                else:
                    idx = match.end()
                    insertion = f"{indent}{start_marker}\n{indent}{end_marker}\n"
                    content = content[:idx] + insertion + content[idx:]
            else:
                return

        # Prepare new volume lines for this plugin
        plugin_header = f"# [{plugin_id}]"
        new_plugin_lines = [f"      {plugin_header}"]
        for v in volumes:
            env_var = v.get("env_var")
            host_default = v.get("host_path", "")
            container_path = v.get("container_path")
            new_plugin_lines.append(f"      - ${{{env_var}:-{host_default}}}:{container_path}")

        # Extract current plugin block
        pattern = re.compile(
            rf"{re.escape(start_marker)}(.*?){re.escape(end_marker)}",
            re.DOTALL
        )
        match = pattern.search(content)
        if not match:
            return

        current_block = match.group(1)

        # Remove existing block for this plugin if present
        sub_pattern = re.compile(
            rf"\s*#\s*\[{re.escape(plugin_id)}\].*?(?=(\s*#\s*\[|\Z))",
            re.DOTALL
        )
        cleaned_block = sub_pattern.sub("", current_block).rstrip()

        # Append new lines
        new_block_body = cleaned_block + "\n" + "\n".join(new_plugin_lines) + "\n      "

        new_content = pattern.sub(f"{start_marker}{new_block_body}{end_marker}", content)

        try:
            with open(compose_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info(f"Successfully updated {compose_path} with volumes for '{plugin_id}'.")
        except Exception as e:
            logger.error(f"Failed to write compose updates to {compose_path}: {e}")

    def _update_compose_environment(self, compose_path, plugin_id, volumes):
        """Inserts the plugin's volume env vars into the compose `environment:` block."""
        if not os.path.exists(compose_path) or not volumes:
            return

        env_pairs = []
        for v in volumes:
            env_var = v.get("env_var")
            container_path = v.get("container_path")
            if env_var and container_path:
                env_pairs.append((env_var, container_path))
        if not env_pairs:
            return

        try:
            with open(compose_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Could not read compose file {compose_path}: {e}")
            return

        env_start = "# --- PLUGIN ENV START ---"
        env_end = "# --- PLUGIN ENV END ---"

        environment_match = re.search(r"(\n\s+environment:\s*\n)", content)
        if not environment_match:
            # The override file may only have volumes:. Create an environment
            # section after the PLUGIN VOLUMES markers so env vars have a home.
            env_start_tmp = "# --- PLUGIN ENV START ---"
            env_end_tmp = "# --- PLUGIN ENV END ---"
            vol_end_marker = "# --- PLUGIN VOLUMES END ---"
            if vol_end_marker in content:
                idx = content.index(vol_end_marker) + len(vol_end_marker)
                insertion = (
                    f"\n    environment:\n"
                    f"      {env_start_tmp}\n"
                    f"      {env_end_tmp}\n"
                )
                content = content[:idx] + insertion + content[idx:]
            else:
                logger.error(f"No `environment:` block found in {compose_path}")
                return

        # Ensure markers exist inside the environment block
        if env_start not in content or env_end not in content:
            env_block_start = environment_match.end()
            # Insert markers right after the environment: line (before following entries)
            insertion = f"      {env_start}\n      {env_end}\n"
            content = content[:env_block_start] + insertion + content[env_block_start:]
            environment_match = None

        env_pattern = re.compile(
            rf"{re.escape(env_start)}(.*?){re.escape(env_end)}",
            re.DOTALL
        )
        match = env_pattern.search(content)
        if not match:
            logger.error(f"Could not locate env markers in {compose_path}")
            return

        current_block = match.group(1)

        # Remove existing block for this plugin if present
        sub_pattern = re.compile(
            rf"\s*#\s*\[{re.escape(plugin_id)}\].*?(?=(\s*#\s*\[|\Z))",
            re.DOTALL
        )
        cleaned_block = sub_pattern.sub("", current_block).rstrip()

        new_plugin_lines = [f"      # [{plugin_id}]"]
        for env_var, container_path in env_pairs:
            new_plugin_lines.append(f"      {env_var}: \"{container_path}\"")
        new_block_body = cleaned_block + "\n" + "\n".join(new_plugin_lines) + "\n      "

        new_content = env_pattern.sub(f"{env_start}{new_block_body}{env_end}", content)

        try:
            with open(compose_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info(f"Successfully updated {compose_path} environment with {len(env_pairs)} entries for '{plugin_id}'.")
        except Exception as e:
            logger.error(f"Failed to write compose environment updates to {compose_path}: {e}")

    def _remove_compose_environment(self, compose_path, plugin_id):
        """Removes the plugin's env block from the compose `environment:` block."""
        if not os.path.exists(compose_path):
            return

        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()

        env_start = "# --- PLUGIN ENV START ---"
        env_end = "# --- PLUGIN ENV END ---"
        pattern = re.compile(
            rf"{re.escape(env_start)}(.*?){re.escape(env_end)}",
            re.DOTALL
        )
        match = pattern.search(content)
        if not match:
            return

        current_block = match.group(1)
        sub_pattern = re.compile(
            rf"\s*#\s*\[{re.escape(plugin_id)}\].*?(?=(\s*#\s*\[|\Z))",
            re.DOTALL
        )
        cleaned_block = sub_pattern.sub("", current_block).rstrip()
        new_block_body = cleaned_block + "\n      "

        new_content = pattern.sub(f"{env_start}{new_block_body}{env_end}", content)

        try:
            with open(compose_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info(f"Removed compose environment entries for '{plugin_id}'.")
        except Exception as e:
            logger.error(f"Failed to update compose environment for '{plugin_id}': {e}")

    def remove_volume_config_from_compose(self, plugin_id):
        compose_file = self.get_plugin_compose_file()
        if not os.path.exists(compose_file):
            return

        with open(compose_file, "r", encoding="utf-8") as f:
            content = f.read()

        sub_pattern = re.compile(
            rf"\s*#\s*\[{re.escape(plugin_id)}\].*?(?=(\s*#\s*\[|\s*#\s*---\s*PLUGIN|\Z))",
            re.DOTALL
        )
        new_content = sub_pattern.sub("", content)

        with open(compose_file, "w", encoding="utf-8") as f:
            f.write(new_content)
        logger.info(f"Removed plugin '{plugin_id}' volume/env entries from {compose_file}.")


plugin_manager = PluginManager()
