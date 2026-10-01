#!/bin/bash
# =============================================================================
#  User Data приватного вузла cps-v3-opcua-server (ЛБ2, варіант 3)
#  Ubuntu 22.04 · OPC UA Industrial Server (asyncua) · TCP __OPCUA_PORT__
#
#  Вузол не має публічної IP: пакети з Інтернету він отримує ЛИШЕ через
#  NAT Gateway у публічній підмережі (маршрут 0.0.0.0/0 -> nat-gw).
#  Тому успішне встановлення пакетів уже є першою перевіркою роботи NAT.
#  Плейсхолдери __...__ підставляє scripts/vpc_builder.py.
# =============================================================================
set -euxo pipefail
exec > >(tee -a /var/log/cps_user_data.log) 2>&1
export DEBIAN_FRONTEND=noninteractive

# 1. Чекаємо на вихід в Інтернет через NAT Gateway (до ~5 хв)
for i in $(seq 1 30); do
  if curl -fsS --max-time 5 -o /dev/null https://pypi.org/simple/; then echo "NAT egress OK"; break; fi
  echo "NAT egress not ready yet ($i)"; sleep 10
done

# 2. Системні пакети та утиліти діагностики
apt-get update -y
apt-get install -y --no-install-recommends python3-venv python3-pip netcat-openbsd traceroute iputils-ping curl

# 3. Ізольоване середовище Python з бібліотекою OPC UA (FreeOpcUa asyncua)
mkdir -p /opt/cps_opcua
python3 -m venv /opt/cps_opcua/venv
/opt/cps_opcua/venv/bin/pip install --no-cache-dir "asyncua>=1.1"

# 4. Код сервера (той самий файл, що й scripts/opcua_server.py)
cat > /opt/cps_opcua/server.py <<'PYEOF'
__OPCUA_SERVER_PY__
PYEOF

# 5. Системний користувач і systemd-сервіс з автоперезапуском
id -u opcua >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin opcua
cat > /etc/systemd/system/cps-opcua.service <<EOF
[Unit]
Description=CPS OPC UA Industrial Server (LB2 variant 3)
After=network-online.target
Wants=network-online.target

[Service]
User=opcua
Environment=OPCUA_PORT=__OPCUA_PORT__
Environment=OPCUA_PATH=__OPCUA_PATH__
Environment=OPCUA_NS=__OPCUA_NS__
ExecStart=/opt/cps_opcua/venv/bin/python /opt/cps_opcua/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now cps-opcua

# 6. Самоперевірка: порт OPC UA відкрито
for i in $(seq 1 20); do
  if ss -ltn | grep -q ":__OPCUA_PORT__ "; then break; fi
  sleep 2
done
ss -ltnp | grep ":__OPCUA_PORT__ "
echo "CPS_OPCUA_READY $(date -Is)" > /var/log/cps_ready.marker
