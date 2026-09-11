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

### Updating (in-UI self-update)

Updating is done from the **Settings &rarr; System &amp; Update &rarr; Self-update**
card. The app talks to the Docker Engine over the mounted host socket, pulls the
new image, `docker create`s a temporary replacement container from your current
container's configuration, and launches a tiny **updater sidecar** image that
swaps them:

```
stop old → rm old → rename new → reconnect networks → start new → verify
```

Because the sidecar is a separate container with its own Docker access, stopping
the old container never interrupts the swap — the whole flow runs and reports
live progress in the browser. No host-side watcher, cron, or terminal is needed.

This requires the host Docker socket mounted **read-write** into the container
(it **is** present in the default `docker-compose.yml`):

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

The self-update card shows a capability badge (`socket: rw` / `socket: ro` /
`socket: missing`) and the image tag it will pull (default
`nakedzombie/sdcodex:latest`, overridable). If the socket is absent or mounted
read-only the swap is disabled with a clear explanation.

> **Security note:** the Docker socket grants code running in the container the
> ability to manage containers/volumes on the host. It is only needed for
> in-UI self-update — if you don't want host Docker access, remove that one
> line from `docker-compose.yml` and the self-update card will report
> `socket: missing`. Plugin updates (Resources, volume mounts) still apply via
> the Plugin Manager's per-plugin update button. The updater sidecar image can
> be pulled (override `SDCODEX_UPDATER_IMAGE`) or built automatically from
> `updater/` when the repo is bind-mounted.

## 🔐 Users & SSO (local auth + OpenID Connect)

By default **authentication is off** — anyone who can reach the web UI can use it
(you just set a Civitai API key). If you'd like to gate the app behind a sign-in,
the **Settings &rarr; Users &amp; SSO** tab lets you:

* **Enable authentication** (a toggle). Until at least one user exists the app
  stays open ("bootstrap mode") so you can't lock yourself out.
* **Local users** — create username/password accounts. Passwords are hashed with
  scrypt (constant-time). Logging in issues an httpOnly session cookie
  (`sameSite=lax`, `Secure` only behind an HTTPS reverse proxy); the Civitai API
  key becomes a per-user setting rather than the login credential.
* **Single sign-on via OIDC** — add one or more OpenID Connect providers
  (Keycloak, Authelia, Authentik, Azure AD, Google…):

  | Field | Meaning |
  |---|---|
  | Issuer URL | e.g. `https://idp.example.com/realms/app` |
  | Redirect URI | `https://sdcodex.example.com/auth/oidc/callback` — register this at your IdP |
  | Scopes | `openid profile email` (default) |
  | Username / email / display name claim | which ID-token/userinfo claims map to a user |
  | Admin claim + value(s) | a group/role claim whose value(s) grant admin automatically |

  The login page shows one button per enabled provider. The flow uses **PKCE
  (S256) + state + nonce**; the code is exchanged server-side with the stored
  verifier, and the userinfo endpoint is merged into the ID-token claims. A
  successful SSO signs in (or first-time signs up) a local user linked to that
  provider.

### Cookie security

Session cookies default to `sameSite=Lax` (required for OIDC redirects) and are
not marked `Secure` on plain HTTP so homelab installs don't silently drop them.
If you're behind an HTTPS reverse proxy that sets `X-Forwarded-Proto: https`,
cookies become `Secure` automatically; you can force the behavior with the
`SDCODEX_COOKIE_SECURE` env var (`true`/`false`). Session length defaults to 24h
(`SDCODEX_SESSION_TIMEOUT` seconds).

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