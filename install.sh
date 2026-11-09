#!/bin/bash
# Install Ghosthand on a Debian-based Linux computer (tested on a Raspberry Pi 5 with Raspberry Pi OS, Debian 12 or 13).
# Run it as your normal user from the project folder: ./install.sh
# It is safe to run again, e.g. after pulling a new version.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
USER_NAME=$(id -un)
USER_ID=$(id -u)
UNITS=(pi-iphone-hid pi-iphone-mirror pi-iphone-web)

if [ "$USER_ID" -eq 0 ]; then
  echo "請用一般使用者執行(需要時會自動用 sudo),不要用 sudo ./install.sh" >&2
  exit 1
fi
step() { printf '\n==> %s\n' "$*"; }

step "安裝系統套件"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  uxplay gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav avahi-daemon avahi-utils bluez \
  python3-dbus python3-gi python3-numpy python3-pil python3-venv

step "建立 Python 環境(.venv)"
[ -d "$DIR/.venv" ] || python3 -m venv --system-site-packages "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade openai

step "設定檔 config.env"
if [ ! -f "$DIR/config.env" ]; then
  cp "$DIR/config.env.example" "$DIR/config.env"
  echo "已建立 config.env(之後可以修改)"
fi
chmod 600 "$DIR/config.env"
chmod +x "$DIR"/*.sh "$DIR"/*.py

step "藍牙:讓這台電腦可以當成 iPhone 的鍵盤和滑鼠"
# bluetoothd's own input plugin would hold the HID channels (L2CAP PSM 17/19).
BLUETOOTHD=$(systemctl cat bluetooth | sed -n 's/^ExecStart=\(\/[^ ]*bluetoothd\).*/\1/p' | head -1)
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
printf '[Service]\nExecStart=\nExecStart=%s --noplugin=input\n' "$BLUETOOTHD" |
  sudo tee /etc/systemd/system/bluetooth.service.d/pi-iphone-hid.conf >/dev/null
# Advertise as a keyboard + mouse combo device.
CONF=/etc/bluetooth/main.conf
sudo cp -n "$CONF" "$CONF.bak-pi-iphone"
if ! grep -q '^Class = 0x0025C0' "$CONF"; then
  sudo sed -i '0,/^\[General\]/s//[General]\nClass = 0x0025C0/' "$CONF"
fi

step "建立系統服務"
sudo loginctl enable-linger "$USER_NAME"  # keeps /run/user/$USER_ID around for UxPlay
sudo tee /etc/systemd/system/pi-iphone-hid.service >/dev/null <<EOF
[Unit]
Description=Ghosthand: Bluetooth keyboard and mouse for the iPhone
After=bluetooth.service
Requires=bluetooth.service

[Service]
# Runs as root: don't leave root-owned __pycache__ files in the project folder.
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -u $DIR/hid_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/pi-iphone-mirror.service >/dev/null <<EOF
[Unit]
Description=Ghosthand: AirPlay receiver for the iPhone screen
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
User=$USER_NAME
Environment=XDG_RUNTIME_DIR=/run/user/$USER_ID
ExecStart=$DIR/start_uxplay.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/pi-iphone-web.service >/dev/null <<EOF
[Unit]
Description=Ghosthand: web console
After=network-online.target pi-iphone-hid.service
Wants=network-online.target

[Service]
User=$USER_NAME
ExecStart=$DIR/.venv/bin/python -u $DIR/web.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

step "啟動服務"
# Stop copies started by hand from this folder, if any.
pkill -f "^/bin/sh $DIR/start_uxplay.sh" 2>/dev/null || true
pkill -x uxplay 2>/dev/null || true
sudo pkill -f "^python3 -u $DIR/hid_server.py" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
sudo systemctl enable --quiet "${UNITS[@]}"
sudo systemctl restart "${UNITS[@]}"
sleep 3
for unit in "${UNITS[@]}"; do
  printf '  %-18s %s\n' "$unit" "$(systemctl is-active "$unit")"
done

PORT=$(sed -n 's/^WEB_PORT=//p' "$DIR/config.env")
IP=$(hostname -I | awk '{print $1}')
cat <<EOF

完成!
  網頁控制台:http://$IP:${PORT:-8080}
  (同一個網路的電腦或手機都能用瀏覽器打開)

接下來請照 README.md 的「iPhone 一次性設定」設定手機:
  1. 開啟 AssistiveTouch,把指標顏色設成黃色、關掉自動隱藏
  2. 在 iPhone 的藍牙設定配對「$(sed -n 's/^BT_NAME=//p' "$DIR/config.env")」
  3. 控制中心 → 螢幕鏡像輸出 → 選「$(sed -n 's/^MIRROR_NAME=//p' "$DIR/config.env")」
EOF
