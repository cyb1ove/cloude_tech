#!/bin/bash
# =============================================================================
#  User Data Bastion Host cps-v3-bastion (ЛБ2, варіант 3) · Ubuntu 22.04
#  Єдина точка входу в VPC. Бастіон лише пересилає SSH-з'єднання (ProxyJump):
#  приватні ключі на ньому НЕ зберігаються, агентне перенаправлення вимкнено.
# =============================================================================
set -euxo pipefail
exec > >(tee -a /var/log/cps_user_data.log) 2>&1
export DEBIAN_FRONTEND=noninteractive

# Утиліти для мережевої діагностики (перевірка портів, трасування)
apt-get update -y
apt-get install -y --no-install-recommends netcat-openbsd traceroute iputils-ping curl

# Посилення конфігурації SSH для бастіону
cat > /etc/ssh/sshd_config.d/99-cps-bastion.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowTcpForwarding yes
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
MaxAuthTries 3
ClientAliveInterval 60
ClientAliveCountMax 3
EOF
sshd -t
systemctl restart ssh

echo "CPS_BASTION_READY $(date -Is)" > /var/log/cps_ready.marker
