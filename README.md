# PiSign

![PiSign](pisign/static/pisign/brand/logo-light.svg)

**Turn a Raspberry Pi and a screen into a remotely managed digital sign.**
PiSign is a Python project with a web dashboard for managing displays and a
lightweight player that rotates through web pages in full-screen Chromium.

It is for clubs, community spaces, small businesses, and anyone who wants to
show scoreboards, calendars, announcements, or dashboards on one or more screens.
The player targets the Raspberry Pi Zero 2 W; keep pages lightweight on its
512 MB of RAM.

Each signed-in user has a private collection of displays. Each display has its
own ordered playlist of HTTP(S) URLs, with a duration for each page. You manage
the playlists in your browser rather than editing files on every Pi.

## How it works

```text
Your browser -- Entra sign-in --> PiSign dashboard on Azure
                                          |
                                    Shared database
                                          |
Raspberry Pi -- HTTPS configuration polls -> PiSign API
      |
      +-- Chromium loads playlist URLs directly from their websites
```

The **server** handles sign-in, display pairing, playlists, and device status.
The **player** runs on each Pi, fetches its configuration, and navigates a single
Chromium kiosk tab through the playlist. The server does not fetch, render, or
stream the displayed pages; those sites must be reachable from the Pi.

A new player shows a short-lived pairing code. Claim it in the dashboard to
attach that device to your account. After pairing, it authenticates to the API
with its own device credential, not your browser session or Microsoft account.

Players poll every 30 seconds, sending a heartbeat and checking for playlist
changes. The dashboard marks a display offline after 90 seconds without contact.
During a server or network outage, the player keeps rotating its last saved
playlist, even after reboot. **This caches the playlist, not the websites**:
uncached remote content still needs a network connection.

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

Open the paired display in the dashboard and add its URLs, order, and durations.
For example, rotate a club scoreboard and calendar for 30 seconds each. Durations
can range from 1 to 86,400 seconds; new entries default to 3,600 seconds, so set
them explicitly for a short loop. Changes arrive on the next successful poll;
slow browser operations can add a delay.

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
URL**. Give each display a distinct name and configure its playlist in the
dashboard. Each Pi needs its own identity: do not copy an already-paired device's
`~/.local/state/pisign` directory into another device's image.

### Updates and recovery

To update a player, stop it, run the following from its checkout, then reboot:

```sh
cd "$HOME/pysign"
git pull --ff-only
.venv/bin/python -m pip install -e '.[player]'
sudo reboot
```

Pairing, cached playlists, and the Chromium profile are stored in
`~/.local/state/pisign`. Chromium diagnostics are in `chromium.log` there;
labwc autostart also writes `player.log`. Rebooting does not erase this state.

Players with remote restart support can be restarted from their display settings
using **Restart player**. This restarts PiSign and Chromium, not the Pi, and keeps
pairing, the cached playlist, and browser cookies. Requests expire after ten
minutes; an offline or older player remains pending until expiry. Refresh the
settings page to see whether the restarted player has reconnected.
Existing players need the one-time manual update above to support this action;
their desktop or X11 launchers do not need changing.

Use **Reset / unpair** in display settings to remotely delete its playlist and
browser cookies, release it from your account, and show a fresh pairing code.
Confirm the destructive action before submitting. Offline or older players
remain pending until the ten-minute delivery expiry; update players first.
The player keeps its server-bound credential to pair again without physical
access. This is the normal action for transferring, retiring, or re-pairing a
working display.

For a lost, stolen, or compromised device, expand **Advanced settings** and choose
**Revoke device access**. This immediately revokes its server credential and
deletes the server entry and playlist without waiting for the player to
cooperate. Browser cookies are not remotely erased.

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
the QR points to the current page URL.

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
