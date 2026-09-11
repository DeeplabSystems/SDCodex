<h1 align="center" style="font-weight: bold;">SDCodex</h1>
<p align="center">
<img src="app/static/img/icon/SDCodex.svg" alt="SDCodex" width="220" height="220">
</p>

**SDCodex** is a modular web application built with Flask for exploring, organizing, and managing Stable Diffusion and Generative AI models. It provides a clean, fast interface to browse Civitai models, organize your local library, and dynamically extend functionality through a **Plugin System**.

---

## 🏛️ Core Features

- **Model Explorer & Civitai Browser:** Browse AI models by type (Checkpoint, LoRA, VAE, etc.), base model (SD 1.5, SDXL, Flux, Pony, Wan Video, etc.), and tags.
- **Library Management:** Organize downloaded models and scan local directories to auto-populate metadata and preview images.
- **Creator Profiles:** Explore creators and see all models associated with them.
- **Download Queue:** Integrated background download manager for model weights and previews.
- **Plugin System:** Install, update, and manage official and community plugins directly from GitHub repositories.

---

## 🧩 Plugin System

SDCodex features an extensible plugin architecture. The core application provides Home, Models, and Library. All additional features are modular plugins that can be installed from within the app:

### Official Plugins

1. **[SDCodex-ComfyCaption](https://github.com/DeeplabSystems/SDCodex-ComfyCaption)**
   - Integrated image gallery, workflow inspector, and dataset tagger.
   - Auto-captioning with vision LLMs and JoyCaption.
   - Custom ComfyUI nodes (`comfyui-sdcodex`) to browse and load SDCodex galleries in workflows.
   - Refer to the [SDCodex-ComfyCaption](https://github.com/DeeplabSystems/SDCodex-ComfyCaption) for model                recommendations and prompt suggestions

2. **[SDCodex-GalleryDL](https://github.com/DeeplabSystems/SDCodex-GalleryDL)**
   - Automated batch image download tasks using `gallery-dl` and `yt-dlp`.
   - Single-URL quick download with real-time log streaming.
   - Slideshow kiosks and OAuth credential management for Reddit, DeviantArt, Tumblr, Flickr, and more.

3. **[SDCodex-RemBG](https://github.com/DeeplabSystems/SDCodex-RemBG)**
   - High-accuracy background removal and replacement powered by BiRefNet.
   - Quick BG Remove, BG Replace (custom colors/images), and Background Batch Processing.

---

## 🚀 Quick Start with Docker Compose

1. **Clone the Core Repository**
   ```bash
   git clone git@github.com:DeeplabSystems/SDCodex.git
   cd SDCodex
   ```

2. **Configure Environment**
   ```bash
   cp env.example .env
   ```
   Configure your host paths in `.env` (e.g. `DB`, `MODELS`, `SDCODEX_PORT`).

3. **Launch SDCodex Stack**
   ```bash
   docker compose up -d
   ```
   Open **`http://localhost:5001`** in your browser.

4. **Install Plugins via UI**
   - Navigate to **Settings** &rarr; **Plugins & Extensions**.
   - Review available plugins from registered GitHub repositories.
   - Click **Install Plugin** on any plugin you want.
   - Confirm or customize the volume mount paths for your host filesystem.
   - The plugin manager writes the volume settings to `.env` and updates `docker-compose.yml`.
   - Rerun:
     ```bash
     docker compose up -d
     ```
   - The newly mounted volumes and features will immediately be available in SDCodex!

### Updating from the web UI

The **Settings &rarr; System &amp; Update** tab has a **Request Update** button.
Because the app runs inside Docker (no `docker`/`docker-compose` inside the
container, and `docker compose down`/`up -d --build` are host operations), the
button only *records* an update request in a shared state file
(`<repo>/db/system_update.json`, bind-mounted as `/data/db`). A small
**host-side** watcher applies it:

```bash
# continuous (in a terminal) — or run the same binary from cron:
scripts/ui-update-watcher.sh --watch

# cron style: check-and-apply once, e.g. every 5 minutes
# */5 * * * * /path/to/SDCodex/scripts/ui-update-watcher.sh --once
```

The watcher runs `update.sh` (`git pull` + `docker compose up -d --build`) on
the host whenever a request is pending, and records the outcome (and git
commit) back into the state file so the web UI shows status/progress.

### Self-update directly in the UI (optional Docker socket)

If you mount the host Docker socket **read-write** into the `sdcodex`
container, the **Settings &rarr; System &amp; Update** tab gains a
**Self-update in the UI** card that can replace this very container entirely
from the browser — no host watcher/cron/terminal needed:

```yaml
# docker-compose.yml → services.sdcodex.volumes (add this line):
- /var/run/docker.sock:/var/run/docker.sock
```

With the socket mounted, the UI (via the Docker Engine API) can pull/build the
new image, `docker create` a temporary replacement container from your
current container's configuration, and launch a tiny **updater sidecar** image
that swaps them:

```
stop old → rm old → rename new → reconnect networks → start new → verify
```

Because the sidecar is a separate container with its own Docker access,
stopping the old container never interrupts the swap — the whole flow runs and
reports live progress in the browser.

The self-update card shows a capability badge (`socket: rw` / `socket: ro` /
`socket: missing`) and the image tag it will pull (default
`nakedzombie/sdcodex:latest`, overridable). If the socket is absent or mounted
read-only the swap is disabled with a clear explanation.

> **Security note:** mounting `docker.sock` grants code running in the container
> the ability to manage containers/volumes on the host. It is **not required**
> for normal use — only enable it if you want in-UI self-update, and keep it
> read-only unless you are actively updating. The updater sidecar image can be
> pulled (override `SDCODEX_UPDATER_IMAGE`) or built automatically from
> `updater/` when the repo is bind-mounted.

### Easy cron setup (`scripts/setup_cron.sh`)

Interactive helper that sets up the cron job for you:

```bash
scripts/setup_cron.sh
```

It prompts for your SDCodex install directory (the `scripts/ui-update-watcher.sh`
path is appended automatically), asks how often to check for updates (minutes,
or a raw cron expression), and installs an idempotent cron entry. Run it as the
user whose Docker + repo access matches how you run `update.sh`.

### Cron setup from the web UI ("System & Update" tab)

The **Settings &rarr; System &amp; Update** tab also has a **Cron setup** card:
choose a frequency (minutes, or a raw cron expression) and click **Save &amp;
Apply on Host**. The web app runs inside the container and can't edit your host
crontab, so a small **host-side agent** applies it. Start it once (it only needs
the frequency from the UI — it auto-detects the install dir):

```bash
scripts/ui-cron-agent.sh --watch     # apply requested frequencies automatically
scripts/ui-cron-agent.sh --once      # check-and-apply once (cron-friendly)
```

---

## 🛠️ Plugin Development

Any GitHub repository can be added as a plugin by including a `plugin.json` manifest at its root:

```json
{
  "id": "my-plugin",
  "name": "My Custom Plugin",
  "version": "1.0.0",
  "description": "Custom feature description",
  "author": "YourName",
  "repository": "https://github.com/username/my-plugin",
  "entrypoint": "plugin:init_plugin",
  "nav_items": [
    {
      "label": "My Feature",
      "url": "/my-feature",
      "icon": "fas fa-star"
    }
  ],
  "volumes": [
    {
      "env_var": "MY_DATA",
      "host_path": "./my_data",
      "container_path": "/data/my_data",
      "description": "Host storage directory for plugin data"
    }
  ]
}
```

In `plugin.py`:
```python
from flask import Blueprint, render_template

my_bp = Blueprint("my_plugin", __name__, template_folder="templates")

@my_bp.route("/my-feature")
def feature_view():
    return render_template("my_feature.html")

def init_plugin(app, db, manifest):
    if "my_plugin" not in app.blueprints:
        app.register_blueprint(my_bp)
```

Add your repository in **Settings &rarr; Plugins & Extensions &rarr; Add Repository** to test and distribute your plugin!

---

## 📜 License
MIT License