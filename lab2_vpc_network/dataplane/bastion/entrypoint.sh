#!/bin/bash
set -euo pipefail
# Маршрут до приватної підмережі через «VPC router» (у AWS це локальний маршрут VPC)
ip route replace 10.30.50.0/24 via 10.30.2.254
# Публічний ключ з cps-lab2-key.pem.pub (приватного ключа на бастіоні немає)
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
install -m 600 -o ubuntu -g ubuntu /keys/authorized_keys /home/ubuntu/.ssh/authorized_keys
ssh-keygen -A >/dev/null
echo "bastion ready"
exec /usr/sbin/sshd -D -e
