# Remote player releases

The platform operator publishes one global desired release. All enrolled devices
(including registered, not-yet-paired devices) receive it on their next successful
authenticated HTTPS configuration poll. Offline devices receive the latest release
when they reconnect; intermediate releases are skipped. There is no account-admin
approval, scheduling, staged rollout, inbound SSH, or separate admin application.
Publication is an operator-only management command, not a dashboard permission.

## Trust and one-time bootstrap

Generate an Ed25519 signing key on a trusted release-building machine:

```sh
openssl genpkey -algorithm Ed25519 -out release-private.pem
chmod 600 release-private.pem
openssl pkey -in release-private.pem -pubout -out release-public.pem
```

Keep the private key offline or in a protected release-signing environment.
Never commit it or put it on players or the server. Distribute the public key
through a trusted provisioning channel and compare its fingerprint before use.
The release server cannot sign code. Key rotation and supervisor upgrades require
local reprovisioning; these releases deliberately cannot replace their trust root.

Existing devices need one local bootstrap from a checkout containing this feature.
Stop the existing player first. Keep its original OS user, server origin,
`--state-dir`, `--name`, and any `--browser` / `--software-rendering` settings.
Use Raspberry Pi OS Bookworm or newer, OS Python 3.10+, `python3-venv`, OpenSSL
with Ed25519 support, and sufficient space for three player environments.
The normal graphical session and Chromium must already work.

```sh
cd "$HOME/pysign"
# Only for this one-time bootstrap: stop playback before updating the checkout.
git pull --ff-only
.venv/bin/python -m pip install -e '.[player]'
.venv/bin/python -m player install-updater \
  --server https://signage.example.com \
  --public-key /trusted/path/release-public.pem
sudo reboot
```

Run bootstrap as the desktop user, **never root**. It needs network access to
install the exact pinned bootstrap dependencies. It copies a fallback player,
creates its own virtualenv, and installs an independent stdlib supervisor and
public key under `~/.local/share/pisign-updater`. The existing desktop/labwc
launchers are replaced with the supervisor launcher, using `/usr/bin/python3`;
custom X11 launchers must instead execute:

```sh
/usr/bin/python3 "$HOME/.local/share/pisign-updater/updater.py" \
  --home "$HOME/.local/share/pisign-updater"
```

Keep only one launcher enabled. The supervisor and player each take a lock.
Bootstrap refuses to overwrite an existing updater installation. A successful
bootstrap no longer depends on the checkout or its virtualenv. Reboot after
bootstrap and check the dashboard shows `bootstrap` rather than version unknown.
Devices without bootstrap continue playback and remote restart as before, but
cannot install releases or report their version.

## Build and publish

Use an increasing positive integer sequence and a unique semantic version.
The sequence, not a lexical version comparison, defines upgrade order.
The builder downloads exact pinned, pure-Python wheels for `websocket-client`
and `segno`, records SHA-256 hashes, packages only player code/resources and
those wheels, then signs the manifest (including archive digest and size).
The player installs dependencies offline with `--require-hashes --no-index`.
Build tools and the OS-provided Python/pip are outside the release.

```sh
python -m tools.build_player_release \
  --version 1.0.0 --sequence 1 \
  --machines aarch64 armv7l armv6l \
  --python 3.11 3.13 \
  --private-key /secure/path/release-private.pem --output /tmp/player-release
```

List **only** machine identifiers (`platform.machine()`) and Python minor versions
actually tested with that release. Compatibility also requires updater protocol 1
and state schema 1. Test the signed artifact on supported Pi/OS combinations
before publication, including offline cached playback and failed-start rollback.
The builder can run on a different architecture because its pinned wheels are
platform-independent; Chromium and Python are never bundled.

On the existing server, mount the trusted public key file read-only, set
`PISIGN_PLAYER_RELEASE_PUBLIC_KEY` to its absolute path, and apply migrations:

```sh
python manage.py migrate
python manage.py publish_player_release /tmp/player-release/player-1.json \
  /tmp/player-release/player-1.tar.gz
```

Publication verifies the signature, size, digest, and increasing sequence, stores
the immutable artifact using the existing private asset storage (local or Azure),
and immediately advertises the highest published sequence to every device.
Use the same database and persistent private storage as the running server.
There is no public artifact URL: downloads require an enrolled device credential.
Serialize publication commands operationally. Retain published artifacts because
a player may still be downloading a previously advertised release.

## Activation, health, and recovery

The existing player poll sends installed version, target, status and error,
and writes the desired signed release to a mailbox outside its cached playlist.
The supervisor independently verifies the signature, compatibility and anti-replay
sequence. It downloads with the server-bound bearer credential, rejects redirects,
checks the exact signed digest/size, rejects unsafe archives, and creates a fresh
virtualenv while the installed player keeps running.

Only after staging succeeds does it stop the old player and Chromium process group
and launch the candidate. A per-launch, fresh heartbeat must show a responsive
playback loop and live Chromium for 30 continuous seconds within 120 seconds.
This is a startup check, not proof every external website works. It also works
with a cached playlist or pairing screen without internet access after download.
On failed startup, the supervisor restores the previous environment automatically.
An interrupted activation rolls back on the next supervisor start; its durable
journal is written before activation. A successfully installed release becomes the
next fallback. No root privileges or reboot are required for normal updates.

Bad signatures never advance the anti-replay floor. Incompatible, invalid,
dependency-install and startup-failing releases are recorded
and not repeatedly attempted, even after reboot. Publish a corrected artifact
with a **new sequence and version**; do not mutate a published release. Transient
network download failures retry after 60 seconds while playback continues.
Staging interrupted by shutdown before activation can also retry; an interrupted
candidate startup rolls back and is not retried.
To revert a fleet intentionally, publish the previous compatible player code as
a new higher sequence/version, rather than replaying an old manifest.

Pairing credentials, playlists, cached assets and Chromium profiles remain in
`~/.local/state/pisign`; activation never resets or migrates them. **Schema 1
releases must read and write state compatible with both the preceding release
and the bootstrap fallback.** Do not perform destructive migrations, browser
profile upgrades, or irreversible server API changes in a player release.
A state-schema change needs a separately designed migration/recovery protocol,
not a changed manifest value.

The dashboard reports the installed version, last update state, target and error
with its last-report time. Offline reports can be stale; refresh after a poll.
Supervisor output goes to `player.log` with the standard labwc launcher; desktop
session output depends on the OS session logger. The updater's `record.json`
retains failed sequences and the current/fallback journal. Release environments
are retained for diagnosis; monitor disk space and remove only unused environments
locally while the supervisor is stopped, never its current or pending release.
Normal remote restart still restarts the player and Chromium, not the supervisor
or the OS. Remote reset remains explicitly destructive and unrelated to updates.

## Explicit installation boundaries

Remote releases update **unprivileged player code and its Python dependencies
only**. The independent supervisor, trust key, OS, Python, Chromium, desktop
configuration and system packages are separately maintained.

The SD-card Wi-Fi service executes the root-owned copy at
`/usr/local/lib/pisign/wifi.py`, installed by `install-wifi.sh`. A release may carry
the player's Wi-Fi status constants/module, but it does **not** replace that
privileged copy, service unit, or NetworkManager settings. Updating the privileged
importer requires a separate trusted local `sudo sh deploy/raspberry-pi/install-wifi.sh`
installation. Never point a root service at a user-writable release or virtualenv.
