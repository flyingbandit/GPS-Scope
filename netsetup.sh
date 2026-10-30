#!/usr/bin/env bash
# GPS Scope network setup (Raspberry Pi OS Bookworm or later, NetworkManager).
#
#   * Every boot: Wi-Fi on and the Pi's own hotspot up, so you can always find it in the field.
#   * Ethernet always works too: plug into your router and open http://<hostname>.local:8080.
#   * The dashboard's Network panel can switch Wi-Fi off until the next reboot, which takes the Pi's
#     2.4 GHz transmitter (and ELRS 2.4 dropouts on the link) out of the test.
#
# Run once from the gpsscope folder, as your normal user:
#   bash netsetup.sh
# Options (environment variables):
#   HOTSPOT_SSID=GPSScope HOTSPOT_PASS=gpsscope123   name/password when a new hotspot is created
#   HOTSPOT_BAND=a  WIFI_COUNTRY=GB                  5 GHz hotspot (Pi 3B+/4/5 only; needs the Wi-Fi country set)
#   HOTSPOT_BAND=bg                                  back to 2.4 GHz
# Undo:  sudo systemctl disable --now gpsscope-net; sudo rm /etc/sudoers.d/gpsscope-net
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user:  bash netsetup.sh   (not with sudo). It asks for sudo when needed."
  exit 1
fi
command -v nmcli >/dev/null || { echo "nmcli not found - this needs NetworkManager (Raspberry Pi OS Bookworm or later)."; exit 1; }
USER_NAME="$USER"
SSID="${HOTSPOT_SSID:-GPSScope}"
PASS="${HOTSPOT_PASS:-gpsscope123}"
BAND="${HOTSPOT_BAND:-}"

if [ -n "${WIFI_COUNTRY:-}" ] && command -v raspi-config >/dev/null; then
  echo "==> Setting Wi-Fi country to $WIFI_COUNTRY"
  sudo raspi-config nonint do_wifi_country "$WIFI_COUNTRY"
fi

echo "==> Looking for an existing hotspot connection"
AP=""
while IFS= read -r line; do
  name="${line%:*}"; type="${line##*:}"; name="${name//\\:/:}"
  [ "$type" = "802-11-wireless" ] || continue
  if [ "$(nmcli -g 802-11-wireless.mode con show "$name" 2>/dev/null)" = "ap" ]; then AP="$name"; break; fi
done < <(nmcli -t -f NAME,TYPE con show)

if [ -n "$AP" ]; then
  echo "   using '$AP'"
else
  [ ${#PASS} -ge 8 ] || { echo "HOTSPOT_PASS must be at least 8 characters"; exit 1; }
  AP="GPSScope-Hotspot"
  echo "==> Creating hotspot '$AP' (SSID $SSID)"
  sudo nmcli con add type wifi ifname wlan0 con-name "$AP" ssid "$SSID" \
    802-11-wireless.mode ap 802-11-wireless.band "${BAND:-bg}" ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp wifi-sec.psk "$PASS" >/dev/null
fi
sudo nmcli con modify "$AP" connection.autoconnect yes connection.autoconnect-priority 100
if [ "$BAND" = "a" ]; then
  if ! iw list 2>/dev/null | grep -q "5180.0 MHz\|5180 MHz"; then
    echo "   This Pi's Wi-Fi has no 5 GHz - keeping 2.4 GHz."
  else
    echo "==> Hotspot on 5 GHz (channel 36)"
    sudo nmcli con modify "$AP" 802-11-wireless.band a 802-11-wireless.channel 36
  fi
elif [ "$BAND" = "bg" ]; then
  echo "==> Hotspot on 2.4 GHz"
  sudo nmcli con modify "$AP" 802-11-wireless.band bg 802-11-wireless.channel 0
fi

echo "==> Installing the Wi-Fi helper and boot service"
printf 'AP_CON=%q\n' "$AP" | sudo tee /etc/gpsscope-net.conf >/dev/null
sudo tee /usr/local/sbin/gpsscope-wifi >/dev/null <<'EOF'
#!/bin/bash
# gpsscope-wifi on|off|boot|status - switches the Pi's Wi-Fi for GPS Scope (installed by netsetup.sh)
AP_CON=""
[ -r /etc/gpsscope-net.conf ] && . /etc/gpsscope-net.conf
case "${1:-}" in
  off)    exec nmcli radio wifi off ;;
  on|boot)
          nmcli radio wifi on
          [ -n "$AP_CON" ] || exit 0
          for i in 1 2 3 4 5; do                       # the radio takes a moment to come up
            nmcli --wait 20 con up "$AP_CON" >/dev/null 2>&1 && exit 0
            sleep 3
          done
          echo "could not bring up hotspot '$AP_CON'" >&2; exit 1 ;;
  status) exec nmcli radio wifi ;;
  *)      echo "usage: gpsscope-wifi on|off|status" >&2; exit 2 ;;
esac
EOF
sudo chmod 755 /usr/local/sbin/gpsscope-wifi

sudo tee /etc/systemd/system/gpsscope-net.service >/dev/null <<'EOF'
[Unit]
Description=GPS Scope network - Wi-Fi on and hotspot up at every boot
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/gpsscope-wifi boot

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable gpsscope-net.service >/dev/null

echo "==> Letting GPS Scope switch Wi-Fi (only this helper, no other sudo rights)"
TMP="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/gpsscope-wifi" >"$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
  sudo install -m 440 "$TMP" /etc/sudoers.d/gpsscope-net
else
  echo "   sudoers check failed - skipped (the dashboard Wi-Fi switch won't work)"
fi
rm -f "$TMP"

echo
echo "Done. Hotspot '$AP' comes up at every boot; Ethernet works alongside it."
echo "On Ethernet: open http://$(hostname).local:8080 > Network to switch Wi-Fi off until the next reboot."
echo "Now:   sudo /usr/local/sbin/gpsscope-wifi on     (brings the hotspot up without rebooting)"
