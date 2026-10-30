#!/usr/bin/env bash
# GPS Scope installer for Raspberry Pi OS (Bookworm or later).
# Run from inside the gpsscope folder:   bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="${SUDO_USER:-$USER}"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user:  bash install.sh   (not with sudo). It asks for sudo when needed."
  exit 1
fi
# scp from Windows can drop write permission (folder arrives as 500) - restore it.
chmod -R u+rwX "$DIR" 2>/dev/null || true
# A folder copied or unpacked as root can't be written by us - take it back.
if [ ! -w "$DIR" ] || find "$DIR" ! -user "$USER_NAME" -print -quit | grep -q .; then
  echo "==> Fixing ownership of $DIR"
  sudo chown -R "$USER_NAME":"$USER_NAME" "$DIR"
fi

echo "==> Installing Python packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> Downloading map library for offline use"
mkdir -p "$DIR/static/vendor"
curl -fsSL https://unpkg.com/leaflet@1.9.4/dist/leaflet.js  -o "$DIR/static/vendor/leaflet.js"  || echo "   (skipped - no internet)"
curl -fsSL https://unpkg.com/leaflet@1.9.4/dist/leaflet.css -o "$DIR/static/vendor/leaflet.css" || true

echo "==> Enabling the hardware UART (GPIO14/15) and disabling the serial console"
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_serial_cons 1 || true   # login shell over serial: OFF
  sudo raspi-config nonint do_serial_hw 0 || true     # serial port hardware: ON
fi
sudo usermod -aG dialout "$USER_NAME"

# Extra GPIO UARTs for more GPS units:  GPS_UARTS=5 bash install.sh   (Pi 4 / Pi 5: up to 5 in total)
NUART="${GPS_UARTS:-1}"
if [ "$NUART" -gt 1 ]; then
  CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
  MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
  echo "==> $MODEL: enabling $NUART UARTs in $CFG"
  case "$MODEL" in
    *"Pi 5"*) OVS="uart1-pi5 uart2-pi5 uart3-pi5 uart4-pi5" ;;
    *"Pi 4"*|*"Compute Module 4"*|*"Pi 400"*) OVS="uart2 uart3 uart4 uart5" ;;
    *) echo "   This Pi has only one reliable GPIO UART - use USB-UART adapters for more GPS units."; OVS="" ;;
  esac
  i=1
  for ov in $OVS; do
    [ "$i" -ge "$NUART" ] && break
    grep -q "^dtoverlay=$ov\b" "$CFG" || echo "dtoverlay=$ov" | sudo tee -a "$CFG" >/dev/null
    i=$((i+1))
  done
fi

PORT_ARGS=""
[ -n "${GPS_PORT:-}" ] && PORT_ARGS="--port $GPS_PORT --scan"
echo "==> Installing systemd service (auto-detects every GPS on the Pi UARTs and USB serial ports)"
sudo tee /etc/systemd/system/gpsscope.service >/dev/null <<EOF
[Unit]
Description=GPS Scope - u-blox interference diagnostics web UI
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/gpsscope.py $PORT_ARGS
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable gpsscope.service

echo
echo "Done. Reboot once so the UART change takes effect:  sudo reboot"
echo "Then open  http://$(hostname).local:8080  (or http://<pi-ip>:8080)"
echo "Logs:      journalctl -u gpsscope -f"
echo "More GPS:  GPS_UARTS=5 bash install.sh   (Pi 4/5 extra GPIO UARTs) - USB-UART adapters are found automatically"
echo "Field use: bash netsetup.sh   (hotspot at every boot, Ethernet alongside, Wi-Fi switch in the dashboard)"
echo "           then join the GPSScope network on your phone and open http://10.42.0.1:8080"
