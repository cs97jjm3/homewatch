#!/bin/bash
# Runs on the Pi. Nothing to download, it just sets the service up and (re)starts it.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
python3 -c "import sqlite3, http.server" || { echo "Python is missing something it needs"; exit 1; }
sed -e "s|__USER__|$(whoami)|" -e "s|__DIR__|$DIR|" whoshome.service \
  | sudo tee /etc/systemd/system/whoshome.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable whoshome > /dev/null 2>&1
sudo systemctl restart whoshome
sleep 4
echo "Service is: $(systemctl is-active whoshome)"
sudo journalctl -u whoshome -n 5 --no-pager
PORT=$(grep -E '^WEB_PORT' config.py | grep -oE '[0-9]+')
echo "Open http://$(hostname -I | awk '{print $1}'):$PORT on your phone or laptop"
