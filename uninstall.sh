#!/bin/bash
# Remove the services and Bluetooth changes made by install.sh. The project folder, config.env
# and runs/ stay; delete the folder yourself if you no longer want them.
set -euo pipefail

UNITS=(pi-iphone-hid pi-iphone-mirror pi-iphone-web)
sudo systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
for unit in "${UNITS[@]}"; do
  sudo rm -f "/etc/systemd/system/$unit.service"
done

sudo rm -f /etc/systemd/system/bluetooth.service.d/pi-iphone-hid.conf
CONF=/etc/bluetooth/main.conf
if [ -f "$CONF.bak-pi-iphone" ]; then
  sudo mv "$CONF.bak-pi-iphone" "$CONF"
fi
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
echo "已移除服務並還原藍牙設定。iPhone 上的「Pi-Agent HID」可以在藍牙設定裡「忘記此裝置」。"
