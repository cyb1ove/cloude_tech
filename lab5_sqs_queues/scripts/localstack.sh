#!/bin/bash
# =============================================================================
#  LocalStack (емулятор AWS) для лабораторних ЛБ2–ЛБ9 · macOS / Linux
#    bash scripts/localstack.sh start | status | logs | stop
#
#  EC2 працює в режимі EC2_VM_MANAGER=mock (ресурси лише в API) — цього достатньо
#  для ЛБ2–ЛБ9. Якщо запущено LocalStack від ЛБ1 (docker VM manager) — його буде перезапущено.
#  Інші сервіси (S3, DynamoDB, SQS, SNS, Lambda, IAM, CloudWatch…) працюють повноцінно.
#  Токен: LOCALSTACK_AUTH_TOKEN або файл .localstack_token (як у ЛБ1).
#  Сумісно з bash 3.2 (macOS).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
CONTAINER="localstack-main"
ENDPOINT="http://localhost:4566"

info() { printf '\033[1;34m[localstack]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[localstack]\033[0m %s\n' "$*" >&2; exit 1; }

check_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker не встановлено: brew install --cask docker, потім відкрийте Docker Desktop."
  docker info >/dev/null 2>&1 || fail "Docker не запущено. Відкрийте Docker Desktop."
}

load_token() {
  if [ -z "${LOCALSTACK_AUTH_TOKEN:-}" ]; then
    for f in .localstack_token ../lab*/.localstack_token; do
      [ -f "$f" ] && LOCALSTACK_AUTH_TOKEN="$(tr -d '[:space:]' < "$f")" && break
    done
  fi
  if [ -z "${LOCALSTACK_AUTH_TOKEN:-}" ]; then
    echo "Потрібен Auth Token LocalStack: https://app.localstack.cloud → Auth Tokens"
    printf "Вставте токен (введення приховано): "; read -r -s LOCALSTACK_AUTH_TOKEN; echo
    [ -n "$LOCALSTACK_AUTH_TOKEN" ] || fail "Токен порожній."
    ( umask 077; printf '%s\n' "$LOCALSTACK_AUTH_TOKEN" > .localstack_token )
  fi
  export LOCALSTACK_AUTH_TOKEN
}

cmd_start() {
  check_docker; load_token
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    mgr="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | sed -n 's/^EC2_VM_MANAGER=//p')"
    if [ "$mgr" = "mock" ]; then
      info "LocalStack уже запущено в режимі EC2 mock."
    else
      info "Запущено LocalStack з EC2_VM_MANAGER=${mgr:-default} (ЛБ1) — перезапускаю в режимі mock."
      docker rm -f "$CONTAINER" >/dev/null
    fi
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    info "Запускаю LocalStack (EC2_VM_MANAGER=mock)..."
    docker run -d --name "$CONTAINER" \
      -p 127.0.0.1:4566:4566 -p 127.0.0.1:4510-4559:4510-4559 \
      -e LOCALSTACK_AUTH_TOKEN -e EC2_VM_MANAGER=mock \
      -v /var/run/docker.sock:/var/run/docker.sock \
      localstack/localstack >/dev/null
  fi
  info "Чекаю готовності (до 120 с)..."
  i=0
  while [ $i -lt 60 ]; do
    if curl -fsS --max-time 2 "$ENDPOINT/_localstack/health" 2>/dev/null | grep -q '"ec2": *"\(available\|running\)"'; then
      info "LocalStack готовий: $ENDPOINT"
      echo "    Далі: bash run_mac.sh --localstack"
      return 0
    fi
    sleep 2; i=$((i + 1))
  done
  docker logs --tail 30 "$CONTAINER" 2>&1 || true
  fail "LocalStack не став готовим (перевірте токен: bash scripts/localstack.sh logs)."
}

case "${1:-}" in
  start)  cmd_start ;;
  status) check_docker
          curl -fsS "$ENDPOINT/_localstack/health" 2>/dev/null | python3 -c \
            "import json,sys;d=json.load(sys.stdin);print('edition:',d.get('edition'),'| version:',d.get('version'),'| ec2:',d.get('services',{}).get('ec2'))" \
            || info "LocalStack не запущено." ;;
  logs)   check_docker; docker logs -f --tail 100 "$CONTAINER" ;;
  stop)   check_docker; docker rm -f "$CONTAINER" >/dev/null 2>&1 && info "LocalStack зупинено." || info "LocalStack не був запущений." ;;
  *) echo "Використання: bash scripts/localstack.sh {start|status|logs|stop}"; exit 1 ;;
esac
