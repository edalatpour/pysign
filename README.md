# PySign

A Python digital signage platform with a Django web UI/API and a lightweight
Chromium player for Raspberry Pi Zero 2 W. Each signed-in consumer owns a private
collection of displays and ordered URL playlists, with a duration per URL.
Authentication uses **Microsoft Entra External ID in an external (consumer/customer)
tenant**, not local passwords, workforce tenants, or Azure AD B2C policies.

## What it does

- Pair a new display using the short-lived code shown on its screen.
- Rename displays; add, edit, order, and remove HTTP(S) URLs and their durations.
- Run one full-screen Chromium kiosk tab, rotating through actual pages rather than
  embedding sites in iframes. One tab limits RAM usage on the Zero 2 W.
- Poll configuration every 30 seconds and apply changed URLs/durations immediately
  after a successful poll. Unchanged playlists do not restart. Polling also sends
  a heartbeat; the dashboard considers a display offline after 90 seconds.
- Keep rotating the last valid playlist during server/network outages, including
  after a reboot. Loading uncached remote pages still needs their network.
- Revoke a device's credential when its owner disconnects it.

Durations are 1 to 86,400 seconds. For a scoreboard/calendar loop, add the scoreboard
at order 1 with duration 5 and the calendar at order 2 with duration 5. Duplicate
order values are resolved consistently by creation order.

## Server: local setup

Requires Python 3.10+.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[server]'
export PYSIGN_DEBUG=1
python manage.py migrate
python manage.py runserver
```

Open `http://localhost:8000`. Configure Entra before signing in. Development mode
permits HTTP and uses a development-only session secret; never expose it publicly.
For testing on your LAN, explicitly set `PYSIGN_ALLOWED_HOSTS` to include the server
hostname/address and bind `runserver 0.0.0.0:8000`.

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
export PYSIGN_ENTRA_TENANT_ID='your-external-directory-uuid'
export PYSIGN_ENTRA_SUBDOMAIN='your-tenant-subdomain'
export PYSIGN_ENTRA_CLIENT_ID='your-application-client-id'
export PYSIGN_ENTRA_CLIENT_SECRET='your-client-secret-value'
export PYSIGN_ENTRA_REDIRECT_URI='http://localhost:8000/accounts/callback/'
```

The discovery endpoint is
`https://<subdomain>.ciamlogin.com/<tenant-id>/v2.0/.well-known/openid-configuration`.
Sign-in uses authorization code flow with PKCE, state, and nonce. The library
validates the signed ID token, issuer, audience, and expiration; the app additionally
checks the tenant. Accounts are identified by `(issuer, subject)`, **never linked
by email**. No Microsoft access/refresh tokens are retained after sign-in.
Sign out ends the PySign session; it does not sign out of all Microsoft applications.
There is deliberately no local password or Django admin sign-in.

## Raspberry Pi player

Use Raspberry Pi OS **with a desktop session**, Chromium, networking, and desktop
auto-login. Raspberry Pi OS Lite alone cannot display a browser; install/configure
a graphical session first. A 32-bit desktop OS is a practical choice for the
Zero 2 W's 512 MB RAM. The player runs as the ordinary desktop user, **not root**.

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

An unconfigured Pi opens a full-screen pairing page. In the web UI, sign in,
choose **Pair a display**, and enter its code and a display name. Codes expire
after 10 minutes and refresh automatically. Add URLs to the paired display;
the player picks them up on its next poll.

For an isolated development LAN only:

```sh
.venv/bin/python -m player run --server http://192.168.1.10:8000 --allow-http
```

### Start automatically at boot

Enable **Desktop Autologin** using `sudo raspi-config`. Then, as the same desktop
user and using the same virtual environment:

```sh
.venv/bin/python -m player install-autostart --server https://signage.example.com
sudo reboot
```

The installer creates `~/.config/autostart/pysign.desktop` for XDG desktops.
On Raspberry Pi OS with labwc, it also appends a launcher to
`~/.config/labwc/autostart`, preserving existing entries. If installing before
labwc itself, rerun the installer after installing the desktop. Multiple launches
are prevented by a local process lock. Disable screen blanking using the OS
display settings so the screen remains on. Do not run competing kiosk sessions.

State is in `~/.local/state/pysign` (override with `--state-dir`). The directory
is mode 0700, and credentials/cache files are atomically written as 0600. The
saved identity is bound to the server origin; it is never sent through redirects
or to the displayed pages. Chromium uses a separate persistent profile in that
directory, so a locally authenticated site can retain its session. Logs:
`chromium.log`, plus `player.log` when launched through labwc.

After disconnection, the screen shows a revoked-credential message rather than
silently re-enrolling. To transfer or re-pair a physical device, stop its player,
remove its old entry in the web UI, and reset locally:

```sh
.venv/bin/python -m player reset --yes
.venv/bin/python -m player run --server https://signage.example.com
```

Reset removes the device identity, cached playlist, and Chromium profile/cookies.
It does not delete the old server entry automatically.

### Playback behavior and limits

Chromium stays in full-screen kiosk mode without tabs, toolbars, or browser chrome.
The player navigates the same tab at each interval; this avoids the extra renderer
memory of preloading several tabs. Timing is measured using a monotonic clock
from the start of navigation, not after a site's load event: slow pages may show
loading or have less usable screen time. Repeating a single URL reloads it at its
configured interval. There is no guarantee of seamless transitions for arbitrary
third-party pages.

Keep pages lightweight for a Zero 2 W; heavy dashboards/video can exceed its RAM.
Chromium restarts after a crash. Polling runs in a separate thread, so slow API
requests do not block playlist timing. Kiosk mode does not bypass login screens,
CAPTCHAs, cookie consent, autoplay restrictions, or third-party site availability.
Hardware boot, HDMI behavior, and performance must be checked on the actual Pi.

## Production deployment

```sh
umask 077
pip install -e '.[server,production]'
export PYSIGN_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
export PYSIGN_ALLOWED_HOSTS='signage.example.com'
export PYSIGN_ENTRA_REDIRECT_URI='https://signage.example.com/accounts/callback/'
# Set all the other PYSIGN_ENTRA_* variables above using your secret manager.
unset PYSIGN_DEBUG
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn pysign.wsgi:application --bind 127.0.0.1:8000 --workers 2
```

Persist `PYSIGN_SECRET_KEY` rather than generating a new value at every boot.
Run Gunicorn under your process supervisor as an unprivileged user. Put it behind
a trusted HTTPS reverse proxy, serve `/static/` from `staticfiles/`, and do not
expose Gunicorn directly. Production defaults require HTTPS and secure cookies,
and enable HSTS. A supervisor must supply the same configuration to management
commands and web workers. Set `PYSIGN_DATABASE` to an absolute, backed-up SQLite
path outside the source/deployment directory.

If the proxy terminates TLS, set `PYSIGN_TRUST_PROXY=1` **only** when it replaces
client-supplied `X-Forwarded-Proto` and only the proxy can reach Gunicorn. Rate
limiting intentionally uses `REMOTE_ADDR`, not untrusted forwarding headers. To
rate-limit individual clients behind a proxy, configure a trusted WSGI/server
integration that supplies the real client address, or enforce client-IP limits at
the proxy. Otherwise clients share the proxy's rate-limit bucket.
Use `PYSIGN_CSRF_TRUSTED_ORIGINS` (comma-separated HTTPS origins) only if your proxy
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
PYSIGN_DEBUG=1 python manage.py test signage.tests player.tests
```

Coverage includes cross-owner access on every management surface, mixed slide
IDs, CSRF, bearer isolation, revocation, expired/single-use pairing, claim rate
limits, real signed OIDC token validation (signature/issuer/audience/nonce/expiry),
email-independent identities, exact five-second rotation, configuration updates,
offline caching, private storage, and autostart generation. A real Entra tenant
and Raspberry Pi are needed for live sign-in and hardware acceptance.
