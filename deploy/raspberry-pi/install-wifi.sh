#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo on the Raspberry Pi." >&2
    exit 1
fi

for tool in /usr/bin/python3 /usr/bin/nmcli /usr/bin/raspi-config; do
    if [ ! -x "$tool" ]; then
        echo "Missing $tool. Use Raspberry Pi OS Bookworm or newer with NetworkManager." >&2
        exit 1
    fi
done
if ! mountpoint -q /boot/firmware; then
    echo "The SD card boot partition must be mounted at /boot/firmware." >&2
    exit 1
fi
if ! systemctl is-active --quiet NetworkManager.service; then
    echo "NetworkManager must be running before installing Wi-Fi provisioning." >&2
    exit 1
fi

directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -o root -g root -m 755 /usr/local/lib/pisign
install -o root -g root -m 644 "$directory/../../player/wifi.py" /usr/local/lib/pisign/wifi.py
install -o root -g root -m 644 "$directory/pisign-wifi.service" /etc/systemd/system/pisign-wifi.service
systemctl daemon-reload
systemctl enable pisign-wifi.service
echo "Installed PiSign SD-card Wi-Fi setup. Imports run on boot, not during installation."
