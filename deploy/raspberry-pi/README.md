# SD-card Wi-Fi configuration

PiSign can import Wi-Fi settings from the SD card's Windows/macOS-readable boot
partition on **any boot**, including after moving a player to another network.
This is optional image preparation: a plain Raspberry Pi OS installation does
not recognize `pisign-wifi.json` without the service below. This repository does
not currently produce a downloadable Raspberry Pi image.

## Prepare a Pi or base image once

Use Raspberry Pi OS **Bookworm or newer**, with NetworkManager, `raspi-config`,
Python 3.10+, and the boot partition mounted at `/boot/firmware`. The importer
targets the built-in adapter, `wlan0`. It supports password-protected
WPA/WPA2-Personal networks (including WPA2-compatible transition networks), not
open, enterprise, or WPA3-only networks. Use a band supported by the Pi; a Zero
2 W needs 2.4 GHz. Networks requiring a web login need separate configuration.

Follow the [player installation instructions](../../README.md#deploy-a-player-to-your-raspberry-pi),
including graphical startup, then run from the repository checkout on the Pi:

```sh
sudo sh deploy/raspberry-pi/install-wifi.sh
```

The installer checks prerequisites, copies the standalone importer to
`/usr/local/lib/pisign/wifi.py` as root-owned, non-user-writable code, and enables
`pisign-wifi.service`. It does **not** change networking immediately, run the
player as root, or grant the player permission to change networking. Rerun the
installer after updating importer code; it deliberately does not execute code
from the player's user-writable checkout or virtualenv as root.
The [remote player updater](UPDATES.md) does not cross this installation boundary:
it cannot replace the root-owned importer, service unit, OS or Chromium.

For a reusable image, install this service **before capturing the image**, and
do not include any device pairing state, Chromium profile, Wi-Fi credentials,
SSH host keys, or other per-device identities in that image. Follow Raspberry
Pi OS image-preparation practices to regenerate machine identities on first
boot. Do not distribute a snapshot of an already-paired player. Preparing the
rest of a distributable image is separate from this installer.

## Configure a new card

1. Flash the prepared PiSign image, then reinsert the SD card into the computer.
2. Open the visible boot partition. Create `pisign-wifi.json` in its root,
   alongside files such as `config.txt` (not inside a new `boot/firmware` folder).
3. Use a plain-text editor to save UTF-8 JSON, replacing the example values:

   ```json
   {
     "ssid": "My Wi-Fi",
     "password": "My Wi-Fi password",
     "country": "GB"
   }
   ```

4. Use the actual uppercase two-letter country code for the Pi's location.
   For a hidden network, add `"hidden": true` as another JSON field.
5. Safely eject the card and boot the Pi. Network setup can take about a minute,
   and longer if a previous connection must be restored. The player starts
   afterward and uses its normal server/pairing flow.

The SSID must be 1 to 32 UTF-8 bytes. The password must be 8 to 63 printable ASCII
characters, or a 64-digit hexadecimal PSK. Spaces are preserved. JSON requires
backslashes and double quotes inside strings to be escaped (`\\` and `\"`).
Unknown fields, duplicate fields, invalid types, and files over 4096 bytes are
rejected. Make sure the filename is not accidentally `pisign-wifi.json.txt`.

The service validates the file before changing the network, sets the regulatory
country with `raspi-config`, and loads a private NetworkManager profile. It
activates that profile with a bounded wait, then enables autoconnect with
priority 10. A successful activation means a local network connection and IP
configuration; it does **not** prove internet or PiSign server availability.
Ethernet alone works normally without a configuration file. Supplying a file
explicitly requests Wi-Fi configuration even when Ethernet is connected.

## Move to another network

Shut the Pi down safely first (for example, `sudo poweroff` through an existing
SSH connection or a local terminal), then disconnect power before removing the
card. Add a new `pisign-wifi.json` using the same steps and boot at the new
location. Do not reflash the card or reset/unpair the player.

Previously saved network profiles are retained. The new profile is preferred
over default-priority profiles; when moving back to a known network, the Pi can
reuse its saved profile. If existing profiles have custom higher priorities,
adjust them locally with NetworkManager. Pairing credentials, playlists,
downloaded assets, and browser cookies are not touched.

## Success, errors, and recovery

- On success, the import file is deleted and `pisign-wifi-status.txt` on the boot
  partition records success. NetworkManager retains the saved credentials in its
  root-only connection profile.
- On failure, the import file remains for correction. The status text explains
  the failed stage without including SSIDs, passwords, or network-tool output.
  Correct the file and reboot, or remove it to skip import on the next boot.
- A newly created profile is initially excluded from autoconnect. If import
  fails, the service attempts to remove that profile and reactivate the previous
  connection, if there was one. Old profiles are never deleted. Cleanup or
  reconnection failures are explicitly reported; the previous network may simply
  be out of range at the new location.
- Errors appear in the boot console and `journalctl -u pisign-wifi.service -b`.
  An unconfigured player also shows Wi-Fi recovery instructions on its screen.
  A paired player with a cached playlist continues playback rather than replacing
  it with the error screen; use the status file or journal for diagnostics.
- No file means no import and no network changes. An ordinary network outage
  does not erase profiles or expose a setup hotspot. The status text is the last
  import result, not a live connectivity indicator.

If needed, connect Ethernet or use a local terminal and NetworkManager to repair
network settings. A power cut during import can leave an extra profile or the
import file for the next boot; avoid interrupting setup. The boot partition must
be writable and the system must have free space.

**Treat the SD card as a credential.** The import file contains a plaintext
password. Deletion is not secure erasure on flash storage, and the saved Wi-Fi
profile remains on the Linux partition. Do not commit the file, upload it, or
share card images containing credentials. The importer does not send Wi-Fi
credentials to PiSign or put passwords in process arguments or application logs.
Physical access to the card can also alter the OS; this feature is not protection
against someone tampering with the device.

## Verification

On a development machine, the importer tests use mocked system commands and do
not change the host's network:

```sh
python -m unittest player.tests.test_wifi
sh -n deploy/raspberry-pi/install-wifi.sh
```

The optional keyfile-parser test also checks profiles against the real
NetworkManager library without connecting to a network. On Debian/Ubuntu/Pi OS,
install `python3-gi` and `gir1.2-nm-1.0`, then run
`/usr/bin/python3 -m unittest player.tests.test_wifi`. Without that optional
library, only that test is skipped.

Before distributing an image, test on each supported Pi/OS combination: initial
boot, wrong password, hidden SSID, supported special characters, Ethernet-only
startup, moving between networks, and returning to a saved network. Verify
pairing and cached playback survive. These hardware checks cannot be replaced
by the mocked tests.
