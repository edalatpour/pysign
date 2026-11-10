# PiSign

![PiSign](pisign/static/pisign/brand/logo-light.svg)

**Turn a Raspberry Pi and a screen into a remotely managed digital sign.**
PiSign is a Python project with a web dashboard for managing displays and a
lightweight player that rotates through web pages in full-screen Chromium.

It is for clubs, community spaces, small businesses, and anyone who wants to
show scoreboards, calendars, announcements, or dashboards on one or more screens.
The player targets the Raspberry Pi Zero 2 W; keep pages lightweight on its
512 MB of RAM.

Each signed-in user has private collections of displays and playlists. A playlist
is an ordered list of HTTP(S) URLs or private uploaded assets, with a duration
for each entry. Assign one playlist to each display; multiple displays can use
the same playlist. You manage
playlists in your browser rather than editing files on every Pi.

## How it works

```text
Your browser -- Entra sign-in --> PiSign dashboard on Azure
                                          |
                                    Shared database
                                          |
Raspberry Pi -- HTTPS configuration polls -> PiSign API
      |
      +-- Chromium loads playlist URLs directly from their websites
      +-- Player downloads assigned private assets through the authenticated API
```

The **server** handles sign-in, display pairing, playlists, and device status.
The **player** runs on each Pi, fetches its configuration, and navigates a single
Chromium kiosk tab through the playlist. The server does not fetch or render
external URLs; those sites must be reachable from the Pi. Uploaded assets are
different: the server stores them privately,
and documents are rasterized by a separate internal conversion service.

A new player shows a short-lived pairing code. Claim it in the dashboard to
attach that device to your account. After pairing, it authenticates to the API
with its own device credential, not your browser session or Microsoft account.

Players poll every 30 seconds, sending a heartbeat and checking for playlist
changes. The dashboard marks a display offline after 90 seconds without contact.
During a server or network outage, the player keeps rotating its last saved
playlist, even after reboot. Downloaded assets work offline; external websites
are not downloaded by the player and uncached remote content still needs a
network connection.

## The server on Azure

PiSign's Django dashboard and API run together in a Linux container on
**Azure Container Apps**, exposed through HTTPS. The deployment includes:

| Service | Purpose |
| --- | --- |
| Microsoft Entra External ID | Consumer sign-up and sign-in through an external tenant; no local passwords. |
| Azure Container Registry | Stores the server images used by the app and jobs. |
| PostgreSQL Flexible Server | Default cloud database, connected through private networking. |
| Azure Key Vault | Holds the Django session secret, Entra client secret, and PostgreSQL password. |
| Managed identity | Gives the app access to registry images and Key Vault without embedded Azure credentials. |
| Log Analytics | Collects application and deployment logs. |
| Container Apps jobs | Run database migrations before rollout and daily cleanup of expired records. |
| Azure Blob Storage | Private uploaded assets, accessible through the authenticated PiSign backend only. |
| Internal document converter | Converts PDF, Word and PowerPoint documents into page images without admin/storage credentials. |

You can instead use an **existing Azure SQL database** with an isolated `pisign`
schema and managed-identity authentication. That path can also reuse an existing
Container Apps environment and registry. Sessions, device records, playlists, and
rate-limit counters live in the selected database, so replicas share state without
sticky sessions or a shared filesystem.

### Deploy your own server

Follow the [Azure deployment guide](deploy/README.md) from the repository root.
It covers the prerequisites, exact commands, secrets, and database options.
The standard path is:

1. Create an Entra External ID web application in an **external tenant**, with a
   sign-up/sign-in user flow associated with it.
2. Configure Azure CLI, Azure Developer CLI (`azd`), PowerShell 7, your
   subscription, and the deployment settings described in the guide.
3. Run `azd up` to provision resources, build the image, run migrations, and
   deploy the server. Choose local Docker builds, Azure-hosted builds, or a
   prebuilt pipeline image as documented.
4. Register the exact HTTPS `/accounts/callback/` URL printed by deployment as a
   **Web redirect URI** in the Entra application.
5. Open the server URL and sign in, then use that same origin for your players.
   For later application releases, use `azd deploy`.

Azure resources incur charges, including when no screen is playing. Deployment
does not create the Entra application or migrate local SQLite data. Existing
Azure SQL deployments require the guide's one-time user/schema setup before
the first runtime deployment.

## Deploy a player to your Raspberry Pi

You need a Pi connected to a display, a reliable power supply, a microSD card,
network access, and the HTTPS URL of a running PiSign server. **The server runs
separately; you do not install Django or Azure tools on the Pi.**

The steps below use **Raspberry Pi OS with Desktop**. A 32-bit installation is
a practical choice for the Zero 2 W. Raspberry Pi OS Lite needs an additional
graphical setup; use the [minimal Bookworm Lite kiosk guide](docs/reference.md#bookworm-lite-minimal-x11-kiosk-on-a-zero-2-w)
instead of the desktop autostart steps below.

### 1. Prepare the Pi and install the player

Boot Raspberry Pi OS, configure networking, and open a terminal as the ordinary
desktop user. Do not run the player as root.

```sh
sudo apt update
sudo apt install git chromium python3-venv
git clone https://github.com/edalatpour/pysign.git "$HOME/pysign"
cd "$HOME/pysign"
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[player]'
```

The project requires Python 3.10 or newer. Some older Raspberry Pi OS releases
call the Chromium package `chromium-browser`; the player detects either
executable.

### Optional: configure Wi-Fi from the SD card

On a prepared PiSign image, create `pisign-wifi.json` on the SD card's visible
boot partition with your `ssid`, `password`, and two-letter `country` code.
The Pi imports it on boot and removes it after connecting. To change networks,
shut down safely and add a new file to the same card; pairing and cached content
are preserved. This requires a one-time service installation on the Pi or base
image, not just a stock Raspberry Pi OS image. See
[SD-card Wi-Fi setup and recovery](deploy/raspberry-pi/README.md) for installation,
the JSON example, supported networks, and credential precautions.

### 2. Start and pair the display

Replace `https://signage.example.com` with your server's HTTPS origin, without
an extra path:

```sh
.venv/bin/python -m player run --server https://signage.example.com
```

Chromium opens a full-screen pairing page. On your computer or phone, open the
same server URL, sign in, choose **Pair a display**, and enter the code and a
recognizable display name. Pairing codes expire after ten minutes and refresh
automatically.

Open **Playlists** in the dashboard to create a playlist and add its URLs, order,
and durations. In a display's settings, select an existing playlist and choose
**Save settings**, or choose **Create a new playlist** to open the creation screen.
Creating a playlist from a display's settings automatically assigns it to that
display. You can assign the same playlist to other displays; edits apply to all
of them. A display without an assigned playlist shows its ready/status screen.
For example, rotate a club scoreboard and calendar for 30 seconds each. Durations
can range from 1 to 86,400 seconds; new entries default to 3,600 seconds, so set
them explicitly for a short loop. Changes arrive on the next successful poll;
slow browser operations can add a delay.
Each URL's **Show QR code** checkbox is checked by default. Uncheck it and save
to omit QR generation and display for that URL; the PiSign logo remains visible.
The optional **QR code URL** can point somewhere different from the displayed
page. Leave it blank to use the current page URL, including redirects.

### Upload images, videos and documents

Open **Assets** to upload once and reuse content in multiple playlists. Supported
formats are JPEG, PNG and static WebP images; MP4 (H.264/AAC) and WebM
(VP8/VP9 with Opus/Vorbis) videos; PDF; Word (`.doc`, `.docx`); and PowerPoint
(`.ppt`, `.pptx`). HTML, SVG, animated images and macro-enabled Office formats
are not supported. Defaults are 100 MiB per upload and 1 GiB stored per account.
Images are limited to 16 megapixels and videos to 4K and 24 hours; lightweight
content is strongly recommended for the Pi Zero 2 W.

Documents are converted into static page images, up to 50 pages, with a maximum
edge of 1920 pixels. Only the rendered pages are retained, not the original
document. Use **Preview document** to check every page: missing fonts and complex
layouts may change, and animations, links and embedded media are not preserved.
Password-protected or unrenderable documents are rejected.

In a playlist entry, enter **either** an external URL **or** select an asset.
For documents, the entry duration applies to **each page**; for images it is
the display duration, and videos autoplay muted and loop for that duration.
For private assets, turn off **Show QR code** or supply an explicit public QR
code URL; PiSign never turns an authenticated download or local file path into a QR code.

Only the asset owner can manage and preview it. Players can download only assets
from their owner's currently assigned playlist. Azure blobs are never public
and no SAS links are exposed. Assets remain in the library when a playlist is
deleted; an asset cannot be deleted while a playlist references it.

Players download assets before playback and retain them for offline use.
Reassignment removes obsolete local asset files on the next successful poll;
reset, unpairing or credential revocation also clears them when observed.
An offline player cannot immediately receive revocation, and already downloaded
copies cannot be remotely retracted. **Update players before assigning asset
entries:** older players support only external URLs.

### 3. Start automatically after reboot

Stop the foreground player with `Ctrl+C`. Run `sudo raspi-config` and enable
**Desktop Autologin**, then install the launcher as the same user:

```sh
cd "$HOME/pysign"
.venv/bin/python -m player install-autostart --server https://signage.example.com
sudo reboot
```

The installer configures desktop autostart and, where available, labwc autostart.
Disable screen blanking in the OS display settings and make sure no competing
kiosk launcher is enabled. After reboot, the display retains its pairing and
saved playlist.

### 4. Add more displays

Repeat the installation and pairing steps on each Pi, using the **same server
URL**. Give each display a distinct name and select its playlist in the
dashboard. Each Pi needs its own identity: do not copy an already-paired device's
`~/.local/state/pisign` directory into another device's image.

### Upgrading from per-display playlists

The database migration combines each account's existing URLs into a playlist
named `default` and assigns all of that account's existing displays to it.
URLs retain their titles, durations, QR settings, and order within each former
display playlist; those playlists are concatenated in display creation order.
Duplicate URLs are kept. You can then create separate playlists and reassign
displays as needed. The player API still returns a single URL list, so no player
update is required for shared playlists.

Resetting/unpairing or revoking a display leaves saved playlists and other
displays unchanged. A playlist can be deleted only after it is no longer assigned
to any display.

### Updates and recovery

For remote updates, install the independent updater once using the
[bootstrap and release guide](deploy/raspberry-pi/UPDATES.md). The platform owner
publishes signed, versioned releases to all enrolled devices automatically.
Online devices receive them on their next successful poll; offline devices
receive the latest release when they reconnect. No account-admin approval or
inbound SSH is required. Releases stage in separate environments, check
compatibility and startup health, and roll back failed startup without resetting
player state. Installed versions and update errors appear in display settings.
Do not run live `git pull` updates on a supervised player.

OS, Chromium, and the separately installed root-owned Wi-Fi importer remain
outside remote player updates.

Pairing, cached playlists, and the Chromium profile are stored in
`~/.local/state/pisign`. Chromium diagnostics are in `chromium.log` there;
labwc autostart also writes `player.log`. Rebooting does not erase this state.

Players with remote restart support can be restarted from their display settings
using **Restart player**. This restarts PiSign and Chromium, not the Pi, and keeps
pairing, the cached playlist, and browser cookies. Requests expire after ten
minutes; an offline or older player remains pending until expiry. Refresh the
settings page to see whether the restarted player has reconnected.
Existing players need current player code to support this action. Remote update
installation additionally needs the one-time supervisor bootstrap above.

Use **Reset / unpair** in display settings to remotely delete its cached playlist and
browser cookies, release it from your account, and show a fresh pairing code.
Confirm the destructive action before submitting. Offline or older players
remain pending until the ten-minute delivery expiry; update players first.
The player keeps its server-bound credential to pair again without physical
access. This is the normal action for transferring, retiring, or re-pairing a
working display. Saved account-level playlists and other displays are unchanged.

For a lost, stolen, or compromised device, expand **Advanced settings** and choose
**Revoke device access**. This immediately revokes its server credential and
deletes the display's server entry without waiting for the player to cooperate.
Saved playlists are kept; browser cookies are not remotely erased.

To change servers or recover after access revocation, stop the player, remove its old display
entry in the dashboard, then reset it locally and pair again:

```sh
.venv/bin/python -m player reset --yes
.venv/bin/python -m player run --server https://signage.example.com
```

**Reset deletes local credentials, the cached playlist, and browser cookies.**
If the server URL changed, rerun `install-autostart` with the new URL too.
For framebuffer/software rendering, detailed logs, and alternative setups,
see the [player reference](docs/reference.md#raspberry-pi-player).

## Playback and security essentials

PiSign displays actual web pages, not iframe embeds. Kiosk mode does not bypass
site logins, cookie consent, CAPTCHAs, or access restrictions. Complex pages and
video may exceed a Zero 2 W's resources, and page transitions are not guaranteed
to be seamless. A PiSign logo and locally generated QR code overlay the content;
the QR points to the current page URL by default, or the entry's custom QR code
URL when provided, unless QR display is disabled for that playlist entry.

Use HTTPS, keep the OS and Chromium patched, and retain the browser sandbox.
Place players on a dedicated signage network: a playlist can point to any HTTP(S)
site the Pi can reach, including internal addresses. Protect pairing codes,
device state, and SD cards. Removing a display revokes its credential on the next
successful poll; an offline device can still play its cached playlist.

## Development and detailed documentation

For local development, the server uses SQLite and needs no Azure infrastructure
or Docker. Sign-in still requires an Entra External ID application.

- [Local server setup and Entra configuration](docs/reference.md#server-local-setup)
- [Azure provisioning, database options, and releases](deploy/README.md)
- [Player behavior and troubleshooting](docs/reference.md#player-state-and-recovery)
- [Existing-installation rename migration](docs/reference.md#updating-an-existing-installation-after-the-rename)
- [Security boundaries](docs/reference.md#security-boundaries) and [test commands](docs/reference.md#verification)
- [Brand assets](docs/reference.md#brand-assets)
