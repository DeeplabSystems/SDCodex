# SDCodex Development Memory

Context notes for ongoing modular-plugin work. Added during the plugin-settings
/ plugin-config / plugin-mounts task.

## Open investigation: "printed activity entries" at the top of the Settings page

The user asked to "move the printed activity entries that are appearing at the
top of the settings page to its own dedicated page in settings call **Logs**".

Investigation (so far) found NO such feature in the current codebase or runtime:

- No "activity"/"recent activity"/"log"-style header rendered on the live
  `/settings` page (checked the running container's rendered HTML).
- No activity/feed template exists in the core app templates
  (`app/templates/settings.html`, `base.html`) — Settings currently has three
  tabs: Download Directories & API, Plugins & Extensions, Library Management.
- No "activity" concept found in the current gallery-dl plugin (fresh public
  repo `/tmp/gdl` and the installed copy at `plugins/gallery-dl`): only
  `_recent_downloads_from_log()` which feeds the *tasks* and *quick-download*
  pages, not Settings.
- The old monolithic app at `/home/naked/dev/SDCodex` also has no activity feed
  on its Settings page.

Conclusion: the "activity entries" the user saw were not in the committed code —
they are either from an earlier/provisional state, a screenshot on their side,
or a feature they want built. When it shows up again, capture the exact page URL
and surrounding HTML before acting. A new "Logs" settings page is the desired
destination; the source feed was never located.

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

- `.env` is gitignored (runtime state). `docker-compose.yml` is committed but
  with plugin blocks driven by install (core baseline stays plugin-free).
- Config files (`.env`, `docker-compose.yml`, `requirements.txt`, `plugins/`)
  are bind-mounted; replacing them while the container runs can diverge the
  container's inode references. Recreate via `update.sh` (`docker compose down
  && up -d --build`) after config changes.
- No password-based `sudo`; use privileged helper containers for root-owned
  file cleanup under `plugins/`.