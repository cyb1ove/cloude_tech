#!/bin/bash
set -euo pipefail
# Приватна підмережа: маршрут за замовчуванням лише через NAT Gateway (0.0.0.0/0 -> nat)
ip route replace default via 10.30.50.254
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
install -m 600 -o ubuntu -g ubuntu /keys/authorized_keys /home/ubuntu/.ssh/authorized_keys
ssh-keygen -A >/dev/null
/usr/sbin/sshd -e
echo "worker ready: starting OPC UA server on :${OPCUA_PORT:-4840}"
exec /opt/cps_opcua/venv/bin/python /opt/cps_opcua/server.py
