# PiSign technical reference

For a project introduction and the standard Raspberry Pi setup, start with the
[README](../README.md). This guide keeps the detailed development, alternative
kiosk, troubleshooting, migration, security, and branding notes in one place.
For cloud provisioning and releases, use the
[Azure deployment guide](../deploy/README.md).
Unless stated otherwise, run commands from the repository root.

## Brand assets

The original PiSign mark combines two leaves with a berry-colored monitor,
content tiles, and a negative-space play symbol. It references Raspberry Pi's
berry/leaf association without reproducing the official Raspberry Pi logo.
It is not an official Raspberry Pi mark or a statement of endorsement; check
applicable trademark requirements before publication.

All artwork is in [`pisign/static/pisign/brand/`](../pisign/static/pisign/brand/).
See the [identity preview](../pisign/static/pisign/brand/brand-preview.png).

| Use | Files |
| --- | --- |
| App listings | `app-icon.svg`, opaque square PNGs at 192, 512, and 1024 px |
| Browser / home screen | `favicon.ico`, `app-icon-32.png`, `app-icon-180.png` |
| Admin / display branding | `logo-light.svg` for light backgrounds; `logo-dark.svg` for dark backgrounds |
| Standalone / single-color mark | `mark.svg`, `mark-ink.svg`, `mark-white.svg` |
| Device boot splash | `splash.svg`, `splash-1920x1080.png`, `splash-3840x2160.png` |

SVG lettering is outlined, so deployment needs no fonts or external resources.
Use the full-color mark at 32 px or larger and the single-color mark at smaller
sizes. Keep clear space of at least one leaf-width around the mark; preserve
its aspect ratio. App icons include safe padding and no pre-rounded corners so
listing platforms can apply their own masks. Palette: berry `#e43e73`, leaf
`#29a574`, deep ink `#142b32`, and off-white `#f4f8f7`.

The admin header, browser icons, and the player's local connecting/pairing/status
pages use these assets. A subtle PiSign watermark also overlays URL content in
the bottom-right corner. The splash
images are ready for a device's boot-splash configuration; they are **not**
automatically installed into Plymouth, firmware, or the desktop. Fit rather
than stretch or crop the 16:9 splash on other display shapes.

To regenerate exports, use a separate design environment with `cairosvg`,
`Pillow`, and `fonttools`, then run `python tools/export_brand.py`. The default
outline source is Liberation Sans Bold at its standard Debian font path;
use `--font /path/to/LiberationSans-Bold.ttf` elsewhere. These are development
tools only, not server/player dependencies.

## Hosted test server

The current Azure deployment is available at:

[https://pisign.braveplant-d8d95ba6.centralus.azurecontainerapps.io](https://pisign.braveplant-d8d95ba6.centralus.azurecontainerapps.io)

With the player installed, connect to it using:

```sh
python -m player run --server https://pisign.braveplant-d8d95ba6.centralus.azurecontainerapps.io
```

For local player testing on macOS, with Python 3.15 installed and Google Chrome
installed in Applications, check your Python version first:

```sh
python3.15 --version
```

Then run these commands from the repository root to create a separate player
virtual environment, install the player, and start it with Chrome's executable
explicitly:

```sh
python3.15 -m venv .venv-player
.venv-player/bin/python -m pip install -e '.[player]'
.venv-player/bin/python -m player run \
  --server https://pisign.braveplant-d8d95ba6.centralus.azurecontainerapps.io \
  --browser "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Calling the virtual environment's Python directly avoids needing activation.
For subsequent starts, run only the final command. After pulling updates that
change player dependencies, rerun the installation command before restarting.
Using `.venv-player` leaves any existing `.venv` untouched. Python 3.15 is used
here for local testing; the player requires Python 3.10 or newer.

The player still requires an explicit `--server`; this URL is documented here,
not hardcoded as its default.

## Server: local setup

Requires Python 3.10+.

The server uses Microsoft Authentication Library (MSAL) for Entra sign-in.
Local development uses SQLite by default; no PostgreSQL server, Docker, or Azure
account is needed. Leave `PISIGN_DATABASE_ENGINE` unset (or set it to `sqlite`).
`PISIGN_DATABASE` can override the local SQLite file path.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[server]'
export PISIGN_DEBUG=1
python manage.py migrate
python manage.py runserver
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=cryptography -e '.[server]'
$env:PISIGN_DEBUG = "1"
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Calling the virtual environment's Python directly avoids PowerShell activation
script restrictions. Set your `PISIGN_ENTRA_*` variables in the same terminal
before starting the server; environment variables are not loaded from a file.

`--only-binary=cryptography` selects a prebuilt cryptography wheel from your
configured package feed instead of requiring a local Rust/C build toolchain
(particularly useful on Windows ARM64). It does not change the package index.

Open `http://localhost:8000`. Configure Entra before signing in. Development mode
permits HTTP and uses a development-only session secret; never expose it publicly.
For testing on your LAN, explicitly set `PISIGN_ALLOWED_HOSTS` to include the server
hostname/address and bind `runserver 0.0.0.0:8000`.

### Updating an existing installation after the rename

The application is now PiSign: the Python package is `pisign`, the player command
is `pisign-player`, and all environment variables use the `PISIGN_` prefix.
Stop running processes, uninstall the previous distribution by its installed
name, and reinstall the appropriate extras. Recreate your environment variables
with the new prefix; the previous prefix is no longer read.
The repository checkout folder does not need to be renamed.

The local SQLite filename and Django model names are unchanged, so existing
local data remains usable. Keep the same session secret and database connection
values under the renamed environment variables when retaining a deployment.

On an existing Pi, stop the player and disable its previous desktop/labwc
autostart entries before installing the new ones. To retain pairing and cached
state, either move the previous state directory to `~/.local/state/pisign` or
pass its existing location with `--state-dir`. Run `install-autostart` again
afterward; do not leave both old and new launchers enabled.

Azure resource and database defaults also use `pisign` now. Applying the new
defaults to an existing deployment creates different resources rather than
renaming them or migrating data. Back up existing data and plan that migration,
or explicitly retain existing resource names and database settings in your
deployment templates. An Entra app's display name can be changed independently;
existing tenant/client IDs, secrets, and callback URLs do not change just because
the application branding changed.

### Entra External ID consumer tenant

1. In your **external tenant**, register a web application. Record its application
   (client) ID, directory (tenant) ID, and your tenant's subdomain (the part before
   `.ciamlogin.com`).
2. Add a **Web** redirect URI of
   `http://localhost:8000/accounts/callback/` for development and your exact HTTPS
   callback URI for production.
3. Create a client secret and securely provide its **value**, not its secret ID.
4. Create a sign-up/sign-in user flow in the external tenant and associate this
   application with that user flow. Configure the consumer identity providers and
   account creation options in Entra.
5. Set:

```sh
export PISIGN_ENTRA_TENANT_ID='your-external-directory-uuid'
export PISIGN_ENTRA_SUBDOMAIN='your-tenant-subdomain'
export PISIGN_ENTRA_CLIENT_ID='your-application-client-id'
export PISIGN_ENTRA_CLIENT_SECRET='your-client-secret-value'
export PISIGN_ENTRA_REDIRECT_URI='http://localhost:8000/accounts/callback/'
```

PowerShell equivalent:

```powershell
$env:PISIGN_ENTRA_TENANT_ID = "your-external-directory-uuid"
$env:PISIGN_ENTRA_SUBDOMAIN = "your-tenant-subdomain"
$env:PISIGN_ENTRA_CLIENT_ID = "your-application-client-id"
$env:PISIGN_ENTRA_CLIENT_SECRET = [System.Net.NetworkCredential]::new(
    "", (Read-Host "Entra client secret VALUE" -AsSecureString)
).Password
$env:PISIGN_ENTRA_REDIRECT_URI = "http://localhost:8000/accounts/callback/"
```

The discovery endpoint is
`https://<subdomain>.ciamlogin.com/<tenant-id>/v2.0/.well-known/openid-configuration`.
Sign-in uses MSAL's authorization code flow with PKCE, state, and nonce. The app
uses `joserfc` to explicitly validate the ID token's RS256 signature against the
discovered signing keys, issuer, audience, expiration, and nonce, and additionally
checks the tenant. Pending sign-ins expire after ten minutes and are consumed on
callback. Accounts are identified by `(issuer, subject)`, **never linked
by email**. No Microsoft access/refresh tokens are retained after sign-in.
Sign out ends the PiSign session; it does not sign out of all Microsoft applications.
There is deliberately no local password or Django admin sign-in.

### Troubleshooting sign-in

Always start local sign-in at `http://localhost:8000` when the registered callback
uses `localhost`. Starting at `127.0.0.1` creates a different browser cookie scope,
so the callback cannot recover the pending session. Do not mix browser profiles
or reuse a callback URL from an earlier attempt.

If sign-in fails, the server logs the rejection stage without exposing tokens or
claim values:

| Log stage | What to check |
| --- | --- |
| `session flow` | Same browser and hostname, cookies enabled, and a fresh sign-in within ten minutes. |
| `client initialization` | Tenant configuration and connectivity to Entra discovery. |
| `token exchange` | Provider error/AADSTS code, client secret, app registration, or rejected OAuth state/nonce. |
| `ID token validation` | The reported claim name, token lifetime, signing keys, or network access to discovery/keys. |
| `account identity` | Tenant claim matches the configured external tenant and the local account is active. |

Only safe provider error identifiers and numeric AADSTS codes are logged; provider
descriptions, raw exceptions, tokens, nonces, and identity values are not.
Development-server access logs strip query strings to avoid logging authorization
codes. When sharing diagnostics, send only the `Entra callback rejected at ...`
line, not a callback URL or token. The MSAL recommendation to use `form_post`
is a warning, not itself a sign-in failure; the app currently uses a GET callback
with SameSite=Lax session cookies.

## Raspberry Pi player

Use Raspberry Pi OS with Chromium, networking, and a graphical session: either a
desktop with desktop auto-login, or the minimal Lite kiosk described below.
Lite alone cannot display a browser; install/configure X first. A 32-bit OS is a practical choice for the
Zero 2 W's 512 MB RAM. The player runs as the ordinary desktop user, **not root**.

The player passes `--no-memcheck` to skip Raspberry Pi OS Chromium's interactive
low-memory warning, which otherwise blocks unattended startup on a Zero 2 W.
This only suppresses the launcher dialog; it does not reduce Chromium's memory
requirements or disable its sandbox. If the warning appears with an older player,
stop the player, pull the latest code, and restart it.

On the Pi, obtain this repository, then:

```sh
sudo apt update
sudo apt install chromium python3-venv
python3 -m venv .venv
.venv/bin/pip install -e '.[player]'
.venv/bin/python -m player run --server https://signage.example.com
```

Some older Raspberry Pi OS releases name the package/executable `chromium-browser`;
the player detects either executable. A custom executable can be selected with
`--browser /path/to/chromium`. The player installation does **not** install Django,
Entra libraries, Selenium, or a second browser engine.

For an X11 framebuffer kiosk, add `--software-rendering`. This explicitly disables
GPU compositing and GPU rasterization, including Raspberry Pi Chromium's
launcher-enabled GPU rasterization, and selects the X11 backend. It does not
disable the browser sandbox. CPU rendering can be slower on complex pages.
Leave this option off for a working GPU-accelerated desktop/Wayland session.

An unconfigured Pi opens a full-screen pairing page. In the web UI, sign in,
choose **Pair a display**, and enter its code and a display name. Codes expire
after 10 minutes and refresh automatically. Add URLs to the paired display;
the player picks them up on its next poll.

For an isolated development LAN only:

```sh
.venv/bin/python -m player run --server http://192.168.1.10:8000 --allow-http
```

### Desktop: start automatically at boot

Enable **Desktop Autologin** using `sudo raspi-config`. Then, as the same desktop
user and using the same virtual environment:

```sh
.venv/bin/python -m player install-autostart --server https://signage.example.com
sudo reboot
```

The installer creates `~/.config/autostart/pisign.desktop` for XDG desktops.
On Raspberry Pi OS with labwc, it also appends a launcher to
`~/.config/labwc/autostart`, preserving existing entries. If installing before
labwc itself, rerun the installer after installing the desktop. Multiple launches
are prevented by a local process lock. Disable screen blanking using the OS
display settings so the screen remains on. Do not run competing kiosk sessions.

### Bookworm Lite: minimal X11 kiosk on a Zero 2 W

This setup uses Xorg, Openbox, and Chromium without a full desktop or LightDM.
The framebuffer driver plus explicit Chromium software rendering was verified
on a Zero 2 W running 32-bit Bookworm with a club website and a live scoreboard
rotating every 30 seconds. Use the framebuffer configuration when X's
`modesetting` driver gives a black picture even though the console works; it is
not necessary to replace a working accelerated desktop.

On a fresh Lite installation, as the ordinary SSH/login user:

```sh
sudo apt update
sudo apt install --no-install-recommends \
  xserver-xorg xserver-xorg-video-fbdev xinit xauth x11-xserver-utils openbox \
  chromium fonts-liberation dbus ca-certificates git python3-venv
git clone https://github.com/edalatpour/pysign.git "$HOME/pysign"
cd "$HOME/pysign"
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[player]'
```

Configure the framebuffer X driver; keep the OS's existing KMS configuration.
Do not add another Device configuration if this framebuffer setup already exists:

```sh
sudo mkdir -p /etc/X11/xorg.conf.d
printf '%s\n' 'Section "Device"' '    Identifier "PiSign framebuffer"' \
  '    Driver "fbdev"' '    Option "fbdev" "/dev/fb0"' 'EndSection' \
  | sudo tee /etc/X11/xorg.conf.d/99-pisign-framebuffer.conf
```

For a fresh installation, create the following X startup script. On an existing
kiosk, back up and edit its script instead of overwriting unrelated startup work.
Replace the server URL below with your server if not using the hosted test server:

```sh
install -d -m 700 "$HOME/.local/state/pisign"
cat > "$HOME/.xinitrc" <<'EOF'
#!/bin/sh
xset s off
xset s noblank
xset -dpms
openbox &
exec dbus-run-session -- "$HOME/pysign/.venv/bin/python" -m player run \
  --software-rendering \
  --server https://pisign.braveplant-d8d95ba6.centralus.azurecontainerapps.io \
  >> "$HOME/.local/state/pisign/player.log" 2>&1
EOF
chmod 700 "$HOME/.xinitrc"
sh -n "$HOME/.xinitrc"
```

Append this block to `~/.profile` **once**. It starts X only on the local first
console, never in SSH. If you have a custom `~/.bash_profile`, ensure it sources
`~/.profile` or put the block in the login startup file it actually uses.

```sh
cat >> "$HOME/.profile" <<'EOF'

if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx -- -nocursor
fi
EOF
sudo raspi-config
```

Select **System Options > Boot / Auto Login > Console Autologin**, not Desktop
Autologin, then run `sudo reboot`. Do **not** also use `install-autostart` for this
kiosk: the X startup script is its launcher. The monitor should show the pairing
page; already-paired devices retain their identity and cached playlist.

On an existing kiosk using a `--browser` software-rendering wrapper, after pulling
this version replace that argument in `~/.xinitrc` with `--software-rendering`.
The player then supplies the same flags without the wrapper. Keep the framebuffer
X configuration and console auto-login; Git does not install those OS settings.
Do not restore temporary diagnostic copies of the player source over an update.

### Player state and recovery

State is in `~/.local/state/pisign` (override with `--state-dir`). The directory
is mode 0700, and credentials/cache files are atomically written as 0600. The
saved identity is bound to the server origin; it is never sent through redirects
or to the displayed pages. Chromium uses a separate persistent profile in that
directory, so a locally authenticated site can retain its session. Logs:
`chromium.log`, plus `player.log` when launched through labwc.
Browser failures include a traceback in the player log output to identify the
failing operation; Chromium still restarts after a five-second delay.
If a busy page times out during a watermark/QR refresh, the player logs a warning
and retries on a later refresh without restarting Chromium. Navigation failures
and lost browser connections still trigger browser recovery.
Navigation commands allow up to 30 seconds for Chromium to respond on slower
devices; other browser commands have a five-second response deadline. These are
browser-control deadlines, not guarantees that a remote page has finished loading.
Playlist edits are fetched by a separate worker every 30 seconds and applied
when playback next processes the update; slow browser commands can add delay.
When a paired display has no URLs, its status heading shows the registered device
name. Name changes are applied on the next configuration poll and cached for
offline use. Older caches without a name show "Ready to display" until a successful
poll supplies the name.
On reboot the player can initially show the cached playlist until its first
successful configuration fetch. Rebooting does not clear pairing or the cache.

After disconnection, the screen shows a revoked-credential message rather than
silently re-enrolling. To transfer or re-pair a physical device, stop its player,
remove its old entry in the web UI, and reset locally:

```sh
.venv/bin/python -m player reset --yes
.venv/bin/python -m player run --server https://signage.example.com
```

Reset removes the device identity, cached playlist, and Chromium profile/cookies.
It does not delete the old server entry automatically.

### Remote player restart

In a display's dashboard settings, **Restart player** queues a restart for that
display only. The action requires its owner's signed-in session and CSRF-protected
POST. The device retrieves the request through its existing authenticated HTTPS
configuration poll; no inbound port, SSH, root access, or new launcher is needed.
This restarts the player and Chromium, **not the operating system**.

The server retains the latest request ID, requesting user, request/expiry times,
and completion time per display. Only one request can be pending; repeated clicks
do not extend its ten-minute expiry. Pending requests survive server restarts.
Requests are not saved in the offline playlist. Offline and older players that
ignore the new field stay pending until expiry; update existing players manually
once using the README instructions before relying on remote restart.

Before restarting, the player stops the polling worker and closes Chromium, then
atomically saves a server-bound receipt in `~/.local/state/pisign/restart.json`,
releases its process lock, and re-executes the original Python invocation. It
aborts the restart explicitly if worker shutdown or receipt persistence fails. This
preserves virtualenv, interpreter/CLI options, foreground/desktop/X11 launchers,
pairing credentials, cached playlist, and browser profile. The new process
acknowledges the request only after Chromium starts successfully and an
authenticated configuration poll succeeds. Duplicate deliveries and failed
acknowledgements do not cause restart loops; acknowledgements are retried while
normal playlist polling continues. The player's clock should be synchronized
for expiry checks and HTTPS.

Refresh display settings to check status. **Completed** means the restarted
process connected to Chromium and acknowledged the request, not that every
playlist website loaded successfully. **Expired without confirmation** does not
prove the process never restarted: its acknowledgement could have been delayed.
A new request can be made after completion or expiry. Local `reset --yes` also
removes the receipt; device removal/revocation otherwise keeps its existing
behavior. Arbitrary shell commands, OS reboot, remote reset, and player updates
are not supported by this action.

### Playback behavior and limits

Chromium stays in full-screen kiosk mode without tabs, toolbars, or browser chrome.
The player navigates the same tab at each interval; this avoids the extra renderer
memory of preloading several tabs. Timing is measured using a monotonic clock
from the start of navigation, not after a site's load event: slow pages may show
loading or have less usable screen time. Repeating a single URL reloads it at its
configured interval. There is no guarantee of seamless transitions for arbitrary
third-party pages.

URL content displays a click-through PiSign watermark: a 100 px-wide logo at 65%
opacity centered above an opaque black-and-white QR code inside the same translucent dark
backing, inset 16 px from the bottom-right corner. The QR opens the current page
URL, including redirects, query strings, and fragments; in-page URL changes are
checked once a second. It is generated locally, with a four-module white quiet
zone and at least three pixels per module (longer URLs need a larger code).
URLs exceeding QR capacity are logged and omit the QR, keeping the logo.
Scanning does not bypass authentication or private-network access requirements.
It does not resize the page or intercept clicks, scrolling, or keyboard
focus. The watermark is installed in Chromium's isolated script world on each
navigation (including redirects and reloads), restored if its element is removed,
and reinstalled after browser restarts. It uses bundled artwork without extra
network requests, iframes, or disabling the site's Content Security Policy.
Local connecting, pairing, and status pages retain their existing branding.

The watermark follows ordinary fullscreen containers, but native fullscreen
video, fullscreen cross-origin frames, browser error pages, and sites using
their own top-layer overlays may cover it. It is unobtrusive branding, not a
tamper-proof overlay.

To run the watermark's real-browser tests, set `PISIGN_TEST_BROWSER` to a
Chromium/Chrome executable and run
`python -m unittest player.tests.test_browser player.tests.test_watermark`.
These tests use headless Chromium and a temporary loopback-only content server;
they do not require pairing, Entra sign-in, or a running PiSign server.

Keep pages lightweight for a Zero 2 W; heavy dashboards/video can exceed its RAM.
Chromium restarts after a crash. Polling runs in a separate thread, so slow API
requests do not block playlist timing. Kiosk mode does not bypass login screens,
CAPTCHAs, cookie consent, autoplay restrictions, or third-party site availability.
Hardware boot, HDMI behavior, and performance must be checked on the actual Pi.

## Production deployment

### Azure Container Apps (recommended)

See [the Azure deployment guide](../deploy/README.md) for a non-root container image,
Bicep provisioning, private Azure PostgreSQL, Key Vault secrets, health probes,
and separate migration/cleanup jobs. Use `azd up` for the automated Central US
deployment or follow the manual Azure CLI steps. Local development continues to use SQLite.
The guide also supports an existing, shared Azure SQL database, using a private
`pisign` schema and a schema-restricted managed identity rather than the server's
administrator credentials. Its database pricing/settings are left unchanged.
Initial SQL user/schema setup is required before the first `azd deploy`.
Cloud sessions, device state, and rate-limit counters live in the selected database so that
replicas share state; no sticky sessions or shared filesystem are required.

The server selects PostgreSQL only when `PISIGN_DATABASE_ENGINE=postgresql`.
Set `PISIGN_POSTGRES_NAME`, `PISIGN_POSTGRES_HOST`, `PISIGN_POSTGRES_USER`, and
`PISIGN_POSTGRES_PASSWORD`; missing values fail startup instead of falling back
to SQLite. Optional settings are `PISIGN_POSTGRES_PORT` (default `5432`),
`PISIGN_POSTGRES_SSLMODE` (default `verify-full`), and
`PISIGN_POSTGRES_SSLROOTCERT` (CA bundle path; supplied by the container).
The `production` extra includes the PostgreSQL driver and Gunicorn and targets
Linux containers, not native Windows development.

### Single-host production with SQLite

```sh
umask 077
pip install -e '.[server,production]'
export PISIGN_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
export PISIGN_ALLOWED_HOSTS='signage.example.com'
export PISIGN_ENTRA_REDIRECT_URI='https://signage.example.com/accounts/callback/'
# Set all the other PISIGN_ENTRA_* variables above using your secret manager.
unset PISIGN_DEBUG
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn pisign.wsgi:application --bind 127.0.0.1:8000 --workers 2
```

Persist `PISIGN_SECRET_KEY` rather than generating a new value at every boot.
Run Gunicorn under your process supervisor as an unprivileged user. Put it behind
a trusted HTTPS reverse proxy; WhiteNoise serves the collected `/static/` files
from `staticfiles/`. Do not
expose Gunicorn directly. Production defaults require HTTPS and secure cookies,
and enable HSTS. A supervisor must supply the same configuration to management
commands and web workers. Set `PISIGN_DATABASE` to an absolute, backed-up SQLite
path outside the source/deployment directory.

If the proxy terminates TLS, set `PISIGN_TRUST_PROXY=1` **only** when it replaces
client-supplied `X-Forwarded-Proto` and only the proxy can reach Gunicorn. Rate
limiting intentionally uses `REMOTE_ADDR`, not untrusted forwarding headers. To
rate-limit individual clients behind a proxy, configure a trusted WSGI/server
integration that supplies the real client address, or enforce client-IP limits at
the proxy. Otherwise clients share the proxy's rate-limit bucket.
For Azure Container Apps specifically, `PISIGN_AZURE_CONTAINER_APPS=1` enables
HTTPS scheme handling and uses only ACA's appended, rightmost `X-Forwarded-For`
address for rate limiting. Enable it only behind ACA ingress, not on a directly
exposed server. Ordinary local development ignores these forwarded headers.
Use `PISIGN_CSRF_TRUSTED_ORIGINS` (comma-separated HTTPS origins) only if your proxy
setup requires additional trusted origins. Avoid logging OIDC callback query
strings or authorization headers.

SQLite is intended for a small fleet on one server with a local persistent disk,
not shared network storage or a high-write multi-server deployment. Back it up
with SQLite's backup API or an equivalent consistent snapshot. Run daily:

```sh
python manage.py cleanup_signage
```

This removes expired rate-limit counters and abandoned unpaired registrations
that have not contacted the server in a day.

## Security boundaries

- Every management lookup is scoped to the signed-in owner. Guessing a UUID or
  substituting a slide ID cannot access or change another account's display.
- Browser sessions cannot act as devices; device bearer credentials cannot act
  as users or select another device. Only SHA-256 digests of random 256-bit
  device tokens are stored on the server.
- Pairing codes contain about 50 bits of randomness, expire in 10 minutes,
  require a signed-in user, and can be claimed only once using an atomic update.
  Treat the visible code as a secret until pairing: anyone who learns it can
  claim an unpaired device during its validity window.
- Management mutations and sign-in initiation require CSRF protection. Device
  endpoints alone are CSRF-exempt and use independent bearer credentials.
  Shared database counters throttle registrations, sign-in starts, and claims.
- Only HTTP(S) display URLs without embedded credentials are accepted. URLs are
  never fetched by the server, so owners cannot use this feature for server-side
  requests. **The Pi does fetch them**: place devices on an isolated signage
  network without access to sensitive internal services. Their owner can point
  them at any HTTP(S) address reachable from the Pi.
- Keep Chromium/OS/Python dependencies patched and retain the browser sandbox.
  The player never uses `--no-sandbox`. The loopback DevTools port and local
  credentials require a dedicated, trusted OS account with no untrusted local
  users/processes. Kiosk mode is not protection against physical access.
- Revocation takes effect on the next successful API poll. A device that is
  offline cannot learn about revocation and can continue its cached playlist;
  power it off or erase its local state when immediate physical revocation is
  required. Protect the server database, secret, backups, and device SD cards.

## Verification

```sh
pip install -e '.[server,player]'
PISIGN_DEBUG=1 python manage.py test signage.tests player.tests
```

For native Windows server verification, activate the local virtual environment,
set `$env:PISIGN_DEBUG = "1"`, and run `python manage.py test signage.tests`.
The player suite requires Linux/Raspberry Pi (`fcntl` is unavailable on Windows).

Coverage includes cross-owner access on every management surface, mixed slide
IDs, CSRF, bearer isolation, revocation, expired/single-use pairing, claim rate
limits, real signed OIDC token validation (signature/issuer/audience/nonce/expiry),
email-independent identities, exact five-second rotation, configuration updates,
offline caching, private storage, and autostart generation. A real Entra tenant
and Raspberry Pi are needed for live sign-in and hardware acceptance.
