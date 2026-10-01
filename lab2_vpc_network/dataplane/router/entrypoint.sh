#!/bin/sh
# Емуляція VPC router + NAT Gateway + правил Worker Security Group
set -eu

PUB_NET=10.30.2.0/24
PRIV_NET=10.30.50.0/24
VPC_NET=10.30.0.0/16
BASTION=10.30.2.10
SERVICE_PORT=4840

PUB_IF=$(ip -o -4 addr show | awk '/ 10\.30\.2\./ {print $2}' | cut -d@ -f1)
PRIV_IF=$(ip -o -4 addr show | awk '/ 10\.30\.50\./ {print $2}' | cut -d@ -f1)
echo "public if: $PUB_IF, private if: $PRIV_IF"

# Вихід в Інтернет — лише через публічну мережу (аналог маршруту NAT -> IGW)
ip route replace default via 10.30.2.1 dev "$PUB_IF"

iptables -F FORWARD
iptables -t nat -F POSTROUTING
iptables -P FORWARD DROP

# Stateful-поведінка Security Group: відповіді на дозволені з'єднання
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Worker SG: SSH, OPC UA та ICMP лише з бастіону
iptables -A FORWARD -s "$BASTION" -d "$PRIV_NET" -p tcp --dport 22 -j ACCEPT
iptables -A FORWARD -s "$BASTION" -d "$PRIV_NET" -p tcp --dport "$SERVICE_PORT" -j ACCEPT
iptables -A FORWARD -s "$BASTION" -d "$PRIV_NET" -p icmp -j ACCEPT

# NAT Gateway: приватна підмережа -> Інтернет із трансляцією адреси джерела (SNAT)
iptables -A FORWARD -i "$PRIV_IF" -o "$PUB_IF" -s "$PRIV_NET" ! -d "$VPC_NET" -j ACCEPT
iptables -t nat -A POSTROUTING -s "$PRIV_NET" ! -d "$VPC_NET" -o "$PUB_IF" -j MASQUERADE

echo "nat-gateway ready"
iptables -S FORWARD
iptables -t nat -S POSTROUTING
trap 'exit 0' TERM INT
while true; do sleep 3600 & wait $!; done
