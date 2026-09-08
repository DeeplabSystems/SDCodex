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
        "name": "ComfyUI Caption & Gallery",
        "description": "Image gallery, auto-captioning with LLMs/JoyCaption, and ComfyUI integration nodes."
    },
    {
        "url": "https://github.com/DeeplabSystems/SDCodex-GalleryDL",
        "name": "GalleryDL & Tasks",
        "description": "Background gallery-dl and yt-dlp task management, quick downloads, kiosks, and OAuth."
    },
    {
        "url": "https://github.com/DeeplabSystems/SDCodex-RemBG",
        "name": "RemBG Background Tools",
        "description": "High-precision background removal, replacement, and batch processing powered by BiRefNet."
    }
]

class PluginManager:
    def __init__(self):
        self.app = None
        self.db = None
        self.plugins_dir = None
        self.loaded_plugins = {}
        self.update_cache = {}

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

    def fetch_remote_manifest(self, repo_url):
        """Fetch plugin.json from GitHub or local workspace fallback."""
        short_name, full_url = self.normalize_github_url(repo_url)
        
        # Check local sibling directory first (for development / offline environments)
        repo_name = short_name.split("/")[-1] if "/" in short_name else short_name
        local_candidates = [
            os.path.join(os.path.dirname(self.plugins_dir), "..", repo_name),
            os.path.join("/home/naked/workspace/deeplabs", repo_name),
            os.path.join("/home/naked/dev", repo_name)
        ]
        for candidate in local_candidates:
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

        # Try fetching from GitHub raw content
        headers = {"User-Agent": "SDCodex-Plugin-Manager/1.0"}
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

        return None

    def check_updates(self):
        """Check all installed plugins for updates from GitHub or local source."""
        results = {}
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
                "remote_manifest": remote_manifest
            }
            
        self.update_cache = results
        return results

    def get_available_uninstalled_plugins(self):
        """Get catalog of plugins from registered repositories that are not yet installed."""
        available = []
        with self.app.app_context():
            from app.models import PluginRepo
            repos = PluginRepo.query.all()
            
            for repo in repos:
                manifest = self.fetch_remote_manifest(repo.repo_url)
                if manifest:
                    plugin_id = manifest.get("id")
                    if plugin_id not in self.loaded_plugins:
                        manifest["_repo_id"] = repo.id
                        manifest["_repo_url"] = repo.repo_url
                        available.append(manifest)
                else:
                    # Generic entry if manifest could not be read
                    short_name, full_url = self.normalize_github_url(repo.repo_url)
                    repo_slug = short_name.split("/")[-1].lower()
                    if not any(repo_slug in pid for pid in self.loaded_plugins):
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
        """Clones/copies plugin to plugins_dir, configures .env and docker-compose.yml."""
        short_name, full_url = self.normalize_github_url(repo_url)
        manifest = self.fetch_remote_manifest(repo_url)
        
        plugin_id = manifest.get("id") if manifest else short_name.split("/")[-1].lower()
        target_dir = os.path.join(self.plugins_dir, plugin_id)

        # 1. Install Code
        if manifest and manifest.get("_source_type") == "local" and os.path.exists(manifest.get("_local_path", "")):
            # Local copy / link for development
            import shutil
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(manifest["_local_path"], target_dir, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        else:
            # Git clone
            if os.path.exists(target_dir):
                cmd = ["git", "-C", target_dir, "pull", "origin", "main"]
            else:
                cmd = ["git", "clone", full_url, target_dir]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0 and not os.path.exists(target_dir):
                # Try master branch or ssh url
                cmd = ["git", "clone", f"git@github.com:{short_name}.git", target_dir]
                subprocess.run(cmd, capture_output=True, text=True)

        # 2. Re-read manifest from installed directory
        installed_manifest_path = os.path.join(target_dir, "plugin.json")
        if os.path.exists(installed_manifest_path):
            with open(installed_manifest_path, "r", encoding="utf-8") as f:
                installed_manifest = json.load(f)
        else:
            installed_manifest = manifest or {"id": plugin_id, "name": plugin_id, "volumes": []}

        # 3. Configure Volumes in .env and docker-compose.yml
        if volume_paths is None:
            volume_paths = {}
            for v in installed_manifest.get("volumes", []):
                env_var = v.get("env_var")
                default_host = v.get("host_path", "")
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
            res = subprocess.run(["git", "-C", plugin_dir, "pull"], capture_output=True, text=True)
            if res.returncode != 0:
                return False, f"Git pull failed: {res.stderr}"
        else:
            # Check if there is a local sibling or re-fetch
            manifest_file = os.path.join(plugin_dir, "plugin.json")
            if os.path.exists(manifest_file):
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                repo_url = manifest.get("repository")
                if repo_url:
                    remote = self.fetch_remote_manifest(repo_url)
                    if remote and remote.get("_source_type") == "local":
                        import shutil
                        shutil.copytree(remote["_local_path"], plugin_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))

        # Clear update cache for this plugin
        if plugin_id in self.update_cache:
            del self.update_cache[plugin_id]

        self.load_plugins()
        return True, f"Plugin '{plugin_id}' updated successfully!"

    def uninstall_plugin(self, plugin_id):
        """Removes plugin directory and removes its volume entries from docker-compose.yml."""
        plugin_dir = os.path.join(self.plugins_dir, plugin_id)
        manifest = self.loaded_plugins.get(plugin_id, {})
        
        # Remove volumes from docker-compose.yml
        self.remove_volume_config_from_compose(plugin_id)

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

    def apply_volume_config(self, manifest, volume_values):
        """Updates .env and docker-compose.yml with volume paths for this plugin."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_file = os.path.join(root_dir, ".env")
        compose_file = os.path.join(root_dir, "docker-compose.yml")

        # 1. Update .env file
        env_updates = {}
        volumes = manifest.get("volumes", [])
        for v in volumes:
            env_var = v.get("env_var")
            val = volume_values.get(env_var, v.get("host_path", "")).strip()
            if env_var and val:
                env_updates[env_var] = val

        self._update_env_file(env_file, env_updates)

        # 2. Update docker-compose.yml
        self._update_compose_volumes(compose_file, manifest.get("id"), volumes)

    def _update_env_file(self, env_path, updates):
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

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
            new_lines.append(line)

        for k, v in updates.items():
            if k not in existing_keys:
                new_lines.append(f"{k}={v}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    def _update_compose_volumes(self, compose_path, plugin_id, volumes):
        if not os.path.exists(compose_path) or not volumes:
            return

        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()

        start_marker = "# --- PLUGIN VOLUMES START ---"
        end_marker = "# --- PLUGIN VOLUMES END ---"

        if start_marker not in content or end_marker not in content:
            # If markers are missing, find volumes: block and insert markers
            match = re.search(r"(\s+volumes:\s*\n)", content)
            if match:
                indent = "      "
                insertion = f"\n{indent}{start_marker}\n{indent}{end_marker}\n"
                idx = match.end()
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

        with open(compose_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    def remove_volume_config_from_compose(self, plugin_id):
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        compose_file = os.path.join(root_dir, "docker-compose.yml")
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


plugin_manager = PluginManager()
