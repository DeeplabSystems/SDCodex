# SDCodex Development Memory

Context notes for ongoing modular-plugin work. Added during the plugin-settings
/ plugin-config / plugin-mounts task.

## Open investigation: "printed activity entries" at the top of the Settings page — RESOLVED

The user asked to "move the printed activity entries that are appearing at the
top of the settings page to its own dedicated page in settings call **Logs**".

**Resolution (confirm).** Those entries are the GalleryDL plugin's transient
**flash messages** set in `SDCodex-GalleryDL/artillery.py` (~67 `flash()` sites:
task created / deleted / duplicated / started / stopped / errors, and one-time
downloads). They only surfaced at the top of Settings because `settings.html`
is the *only* template that calls `get_flashed_messages` (`app/templates/
settings.html:14`); every other page (including the gallery-dl tasks page)
never renders them, so they sat in the session and piled up on the next
Settings visit.

**Implementation (built).** Added a **persistent activity log** so these
events survive redirects and are reviewable on a dedicated Logs page:
- `artillery.py` `_activity(level, slug, message)` appends timestamped events
  to `<CONFIG_DIR>/activity.log` (thread-safe, capped to
  `ACTIVITY_LOG_MAX_LINES`=2000). Hooked into every task-action flash site
  (create/update, duplicate, delete, run, pause, stop, clear_logs,
  delete_archive, delete_cookies), the background runner lifecycle
  (start / finished / stopped / timed-out / non-zero / crash), and one-time
  downloads (start / finish / error).
- New routes: `GET /logs` (page), `GET /logs/data` (JSON, newest-first),
  `GET /logs/download`, `POST /logs/clear`. Template `templates/logs.html`.
- Nav: GalleryDL dropdown now has a **Logs** item (`/logs`). Version bumped
  1.1.0 -> 1.2.0 (plugin.json + store plugins.json +
  `DEFAULT_PLUGIN_MANIFESTS` `gallery-dl`), per the versioning convention.
- `CONFIG_DIR` is the config volume, which is where `activity.log` lives.

Note: the per-task `logs.txt` progress streams already exist under
`/tasks/<slug>/logs` (SSE) — the new Logs page is the *audit* layer on top.

## Per-plugin dedicated settings pages (design)

Each installed plugin should have its own dedicated settings page. Mechanism:
a `settings` key in the plugin manifest.

- Manifest field (gallery-dl example):
  ```json
  "settings": { "label": "Config & Scheduler", "url": "/config", "icon": "fas fa-sliders-h" }
  ```
- `get_installed_plugins()` returns `dict(manifest)` copies, so a `settings`
  field automatically reaches `plugin.settings` in `settings.html`.
- Core `settings.html` plugin card shows a **Settings** button when
  `plugin.settings` exists, linking to its URL.
- The core's `DEFAULT_PLUGIN_MANIFESTS` in `app/plugin_manager.py` should mirror
  the same `settings` field so it is available even before/without a fetch, and
  the plugin repo's `plugin.json` is the walking source of truth on install.

### gallery-dl config editor (the "missing settings page")

The gallery-dl plugin's `/config` route (`plugins/gallery-dl/artillery.py`
`config_page`) currently just redirects to a non-existent
`settings#system-config` tab. The full editor template `templates/config.html`
(CodeMirror JSON editor + gallery-dl.conf download/edit, tool versions,
extractor updates, task scheduler, backup/restore) is orphaned. Restoring
`config_page` to `render_template("config.html", ...)` brings back
gallery-dl.conf download/edit.

- All helpers it needs already exist: `CONFIG_FILE`, `DEFAULT_CONFIG_URL`,
  `ensure_data_dirs`, `read_text`, `write_text`, `_set_task_max_concurrent`,
  `_task_max_concurrent`, `load_tasks`, `_get_tool_version`, and the API
  endpoints `/api/config/check-update`, `/api/config/apply-update`,
  `/api/tools/check-update`, `/api/tools/update`.
- `config.html` GET context expected: `config_text`, `config_path`,
  `config_error_line`, `config_error_col`, `gdl_version`, `ytdlp_version`,
  `task_concurrent_max`, `tasks`.
- POST actions on `/config`: `save` (write config_text), `reset` (download
  default from GitHub), `task_settings` (concurrency).
- Core app has **no CSRF** (`settings`/`settings.html` forms POST without a
  token), so the plain forms and `fetch(...)` calls in `config.html` work.
- The core `base.html` has no `main_class` block, but plugin templates using
  `{% block main_class %}` simply don't emit it — harmless.

## HuggingFace mount / ${HF_HOME} vs ${HFH} (rembg)

The rembg plugin's HuggingFace cache mount was never created because the plugin
volume env var was named `HF_HOME`, which collides with the system/HuggingFace
`HF_HOME` env var during Compose interpolation. Fix: rename the plugin's own
var to `HFH`.

- Manifest (`app/plugin_manager.py` default + `plugins/rembg/plugin.json`):
  `volumes[0].env_var` `HF_HOME` -> `HFH` (host `./Huggingface`,
  container `/data/huggingface`).
- `.env`: `HF_HOME=./Huggingface` -> `HFH=./Huggingface`.
- Plugin code (`plugins/rembg/rembg.py`) does not read `HF_HOME` directly —
  it relies on `huggingface_hub`/transformers' default cache. Must derive the
  internal `HF_HOME` from the `HFH` env var before `from_pretrained` so the
  mount (/data/huggingface) is actually used:
  ```python
  hf_home = os.environ.get("HFH")
  if hf_home:
      os.environ["HF_HOME"] = hf_home  # /data/huggingface
  ```
- Because the plugin repo gets cloned on install, the fix must be committed to
  the **remote** `SDCodex-RemBG` repo (`plugin.json` + `rembg.py` + README) and
  to the core `DEFAULT_PLUGIN_MANIFESTS`, then reinstall to regenerate compose
  `.env`/volume entries with `HFH`.

## Install messaging wording

User prefers that install/update messaging reference their wrapper script,
**`run update.sh`**, instead of the raw `docker compose up -d`. Update any flash
messages and doc alerts accordingly.

## Operational notes

- `.env` is gitignored (runtime state).
- **Plugin mounts live in `docker-compose.override.yml`**, never in
  `docker-compose.yml`. The core `docker-compose.yml` stays core-only and is not
  modified at install/uninstall. Docker Compose auto-merges the override file on
  `up`. `apply_volume_config` / `uninstall_plugin` /
  `remove_volume_config_from_compose` target the override via
  `get_plugin_compose_file()`. `_update_compose_environment` creates an
  `environment:` block in the override if a clone only has `volumes:`.
- `update.sh` marks `docker-compose.override.yml` and `requirements.txt` as
  `git update-index --skip-worktree` so runtime plugin writes to them never show
  as git changes (this resolved the git-conflict issue). Their committed baseline
  stays core-only (empty markers / core packages); the working files carry the
  runtime plugin entries.
- `docker-compose.yml` bind-mounts the correctly-spelled
  `./docker-compose.override.yml:/app/docker-compose.override.yml` so the manager
  (running in the container at `/app`) writes the same file Compose reads on the
  host.
- Config files (`.env`, `docker-compose.override.yml`, `requirements.txt`,
  `plugins/`) are bind-mounted; replacing them while the container runs can
  diverge the container's inode references. Recreate via `update.sh`
  (`docker compose down && up -d --build`) after config changes.
- No password-based `sudo`; use privileged helper containers for root-owned
  file cleanup under `plugins/`.

## Plugin Store (JSON-driven catalog)

The "GitHub Plugin Repositories" settings section became **"GitHub Store
Repositories"** pointing at the **SDCodex-Plugin-Store** repo
(`https://github.com/DeeplabSystems/SDCodex-Plugin-Store`). Publishing a new
plugin now only requires adding an entry to that repo's `plugins.json` — no
core code changes.

- `DEFAULT_PLUGIN_STORE_URL` / `PLUGIN_STORE_MANIFEST_FILE` (`plugins.json`) in
  `app/plugin_manager.py`. `PLUGIN_STORE_URL` env var overrides the store URL.
- `fetch_store_manifest()` reads the store JSON from a local clone first
  (`/home/naked/workspace/deeplabs/SDCodex-Plugin-Store`, via
  `LOCAL_PLUGIN_STORE_DIR` or the sibling/workspace heuristics), then from
  GitHub raw / API (auth with `GITHUB_TOKEN` for private stores).
- `get_store_catalog()` returns `{store, plugins}`; falls back to
  `DEFAULT_PLUGIN_REPOSITORIES` if the store can't be reached (resilience).
- `get_available_uninstalled_plugins()` is now driven by the store JSON (each
  entry's full `plugin.json` is fetched for the install manifest); registered
  `PluginRepo` rows are a legacy fallback only.
- `check_updates()` returns `{update_cache, new_plugins, store}`. It compares
  installed versions against each plugin's remote manifest and lists store
  entries not installed as `new_plugins`. `self.store_info` caches the catalog,
  `new_plugins`, and `last_checked` for the Settings UI.
- The Settings POST action `check_plugin_updates` flashes a summary (updates
  available / new plugins / store unreachable).
- The old `add_plugin_repo` / `remove_plugin_repo` actions still exist and are
  exposed under an "advanced" `<details>` in the store section, and the settings
  GET renders store status lightly (no per-plugin version lookups on GET).

### plugins.json schema (`SDCodex-Plugin-Store/plugins.json`)
```json
{
  "name": "SDCodex Plugin Store",
  "description": "...",
  "version": "1.0.0",
  "plugins": [
    { "id": "...", "name": "...", "description": "...",
      "repository": "https://github.com/Owner/Repo", "version": "1.0.0" }
  ]
}
```
Each `plugins[]` entry maps a plugin id to its GitHub repo. On install and for
version checks, the per-repo `plugin.json` is still fetched for the full
manifest (volumes, nav, entrypoint).

## Per-plugin Settings tabs (gallery-dl config lives in Settings)

Plugin settings pages are **tabs inside the SDCodex Settings page**, not
top-nav dropdown items. The gallery-dl "Config" item was removed from its nav
dropdown; the gear on the plugin card opens Settings `?tab=plugin-<id>`.

- Mechanism: a `settings` key in the plugin manifest:
  `{ "label": "...", "url": "...", "icon": "..." }`. Any enabled installed
  plugin with `settings` gets a Settings tab (id `tab-plugin-<id>`,
  `active_tab == 'plugin-<id>'`).
- The Settings tab body is an **iframe** embedding `settings.url?embed=1`.
  The plugin route must honor `?embed=1` and render a chrome-free fragment.
- gallery-dl config.html was split into `_config_content.html` (the content
  partial), `config.html` (extends base.html, includes the partial), and
  `config_embed.html` (fragment, no base chrome). `config_page` serves the
  embed fragment when `?embed=1` is present; the full page still works when
  hit directly.
- The plugin-card Settings button uses
  `url_for('main.settings', tab='plugin-' + plugin.id)`.
## Plugin versioning (update-detection)

**Lesson:** code changes to a plugin must be accompanied by a **version bump** in
the plugin's `plugin.json`, or `check_updates` will never flag an update (it
compares installed vs remote `version`, and if both stay `1.0.0` nothing looks
changed even though code moved). This caused "nothing changed / still on v1".

Convention:
- Bump `version` in the plugin repo's `plugin.json` whenever behavior changes.
- Mirror the same version in the **store** `plugins.json` entry for that plugin.
- Mirror it in the core `DEFAULT_PLUGIN_MANIFESTS` entry (offline/fallback).
- The user pulls plugin changes via the Settings -> plugin card **Update** button
  (git pull / reinstall), not the root `update.sh` (which only updates the core
  repo + rebuilds the image). After a plugin update, restart/`update.sh` reloads
  the new version.

All three official plugins were bumped 1.0.0 -> 1.1.0 to cover prior unreleased
changes (gallery-dl config editor + Settings tab, rembg HFH mount fix,
comfy-caption gallery/custom-nodes updates). Verified: card shows v1.1.0,
"Check for Updates" reports up to date, and a simulated newer remote version
correctly flags `has_update=True`.
