#!/bin/bash
# =============================================================================
#  Docker-стенд data plane (bastion / nat-gateway / OPC UA worker) для LocalStack
#    bash scripts/stand.sh up | status | logs [сервіс] | shell <bastion|worker|nat-gateway> | down
#  Зазвичай його піднімає deploy_network.py --localstack автоматично.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f dataplane/docker-compose.yml"

fail() { printf '\033[1;31m[stand]\033[0m %s\n' "$*" >&2; exit 1; }
docker info >/dev/null 2>&1 || fail "Docker не запущено. Відкрийте Docker Desktop."

case "${1:-}" in
  up)
    [ -f cps-lab2-key.pem.pub ] || fail "Немає cps-lab2-key.pem.pub — спершу виконайте: python3 deploy_network.py --localstack"
    $COMPOSE up -d --build ;;
  status)
    $COMPOSE ps
    echo; echo "Правила nat-gateway (емуляція NAT + Worker SG):"
    $COMPOSE exec -T nat-gateway iptables -S FORWARD 2>/dev/null || true
    $COMPOSE exec -T nat-gateway iptables -t nat -S POSTROUTING 2>/dev/null || true ;;
  logs)   $COMPOSE logs --tail 100 ${2:-} ;;
  shell)  $COMPOSE exec "${2:-bastion}" bash 2>/dev/null || $COMPOSE exec "${2:-bastion}" sh ;;
  down)   $COMPOSE down -v --remove-orphans ;;
  *) echo "Використання: bash scripts/stand.sh {up|status|logs [сервіс]|shell <сервіс>|down}"; exit 1 ;;
esac
