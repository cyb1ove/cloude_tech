#!/bin/bash
# =============================================================================
#  Керування емулятором LocalStack для ЛБ1 (варіант 3) на macOS / Linux.
#
#    bash scripts/localstack.sh start    # підготувати AMI Debian 12 і запустити LocalStack
#    bash scripts/localstack.sh status   # стан сервісів і контейнерів-«інстансів»
#    bash scripts/localstack.sh logs     # журнал LocalStack (Ctrl+C — вихід)
#    bash scripts/localstack.sh stop     # зупинити LocalStack і прибрати контейнери EC2
#
#  Потрібно: Docker Desktop (запущений) і безкоштовний Auth Token LocalStack
#  (app.localstack.cloud → Auth Tokens). Токен береться зі змінної
#  LOCALSTACK_AUTH_TOKEN або з файлу .localstack_token у корені проєкту.
#  Сумісно з bash 3.2 (macOS).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER="localstack-main"
ENDPOINT="http://localhost:4566"
# Параметри AMI беремо з config/settings.json (блок "localstack")
read_cfg() {
  python3 -c "import json;print(json.load(open('config/settings.json'))['localstack']['$1'])" 2>/dev/null || echo "$2"
}
AMI_ID="$(read_cfg ami_id ami-000003)"
AMI_NAME="$(read_cfg ami_name debian-12-cps)"
BASE_IMAGE="$(read_cfg docker_image debian:12)"
AMI_TAG="localstack-ec2/${AMI_NAME}:${AMI_ID}"

info() { printf '\033[1;34m[localstack]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[localstack]\033[0m %s\n' "$*" >&2; exit 1; }

check_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker не встановлено. macOS: brew install --cask docker, потім відкрийте Docker Desktop."
  docker info >/dev/null 2>&1 || fail "Docker не запущено. Відкрийте Docker Desktop і дочекайтесь статусу 'Engine running'."
}

load_token() {
  if [ -z "${LOCALSTACK_AUTH_TOKEN:-}" ] && [ -f .localstack_token ]; then
    LOCALSTACK_AUTH_TOKEN="$(tr -d '[:space:]' < .localstack_token)"
  fi
  if [ -z "${LOCALSTACK_AUTH_TOKEN:-}" ]; then
    echo "Потрібен Auth Token LocalStack (безкоштовний): https://app.localstack.cloud → Auth Tokens"
    printf "Вставте токен (введення приховано): "
    read -r -s LOCALSTACK_AUTH_TOKEN; echo
    [ -n "$LOCALSTACK_AUTH_TOKEN" ] || fail "Токен порожній."
    ( umask 077; printf '%s\n' "$LOCALSTACK_AUTH_TOKEN" > .localstack_token )
    info "Токен збережено в .localstack_token (файл у .gitignore)."
  fi
  export LOCALSTACK_AUTH_TOKEN
}

wait_healthy() {
  info "Чекаю готовності LocalStack (до 120 с)..."
  i=0
  while [ $i -lt 60 ]; do
    if curl -fsS --max-time 2 "$ENDPOINT/_localstack/health" 2>/dev/null | grep -q '"ec2": *"\(available\|running\)"'; then
      info "LocalStack готовий: $ENDPOINT (EC2 доступний)."
      return 0
    fi
    sleep 2; i=$((i + 1))
  done
  docker logs --tail 30 "$CONTAINER" 2>&1 || true
  fail "LocalStack не став готовим. Перевірте токен і журнал: bash scripts/localstack.sh logs"
}

cmd_start() {
  check_docker
  load_token

  # 1. AMI для EC2 у LocalStack — це Docker-образ з тегом localstack-ec2/<name>:<ami-id>.
  #    Готуємо його ДО старту LocalStack, щоб образ точно було зареєстровано як AMI.
  info "Готую AMI ${AMI_ID}: ${BASE_IMAGE} -> ${AMI_TAG}"
  docker pull -q "$BASE_IMAGE" >/dev/null
  docker tag "$BASE_IMAGE" "$AMI_TAG"

  # 2. Запуск LocalStack (одноразовий контейнер, Docker VM manager для EC2)
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    info "LocalStack уже запущено (контейнер $CONTAINER)."
  else
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    info "Запускаю LocalStack (образ localstack/localstack)..."
    docker run -d --name "$CONTAINER" \
      -p 127.0.0.1:4566:4566 \
      -p 127.0.0.1:4510-4559:4510-4559 \
      -e LOCALSTACK_AUTH_TOKEN \
      -e EC2_VM_MANAGER=docker \
      -e EC2_DOWNLOAD_DEFAULT_IMAGES=0 \
      -v /var/run/docker.sock:/var/run/docker.sock \
      localstack/localstack >/dev/null
  fi
  wait_healthy

  # 3. Перевірка, що AMI видно через API EC2
  if AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 \
     python3 - "$ENDPOINT" "$AMI_ID" <<'PY' 2>/dev/null
import json, sys, urllib.request, urllib.parse
# Мінімальний запит EC2 Query API без boto3 (LocalStack не перевіряє підпис)
endpoint, ami = sys.argv[1], sys.argv[2]
data = urllib.parse.urlencode({"Action": "DescribeImages", "Version": "2016-11-15",
                               "ImageId.1": ami}).encode()
req = urllib.request.Request(endpoint, data=data, headers={
    "Authorization": "AWS4-HMAC-SHA256 Credential=test/20260101/us-east-1/ec2/aws4_request, SignedHeaders=host, Signature=x"})
body = urllib.request.urlopen(req, timeout=10).read().decode()
sys.exit(0 if ami in body else 1)
PY
  then
    info "AMI ${AMI_ID} зареєстровано в LocalStack."
  else
    info "УВАГА: AMI ${AMI_ID} поки не видно в DescribeImages. Якщо main.py повідомить про це —"
    info "виконайте: bash scripts/localstack.sh stop && bash scripts/localstack.sh start"
  fi

  echo
  info "Готово. Запуск лабораторної в режимі емулятора:"
  echo "    bash run_mac.sh --localstack --keep"
}

cmd_stop() {
  check_docker
  info "Прибираю контейнери-«інстанси» EC2..."
  ids="$(docker ps -aq --filter "ancestor=${AMI_TAG}" 2>/dev/null || true)"
  [ -n "$ids" ] && docker rm -f $ids >/dev/null
  docker rm -f "$CONTAINER" >/dev/null 2>&1 && info "LocalStack зупинено." || info "LocalStack не був запущений."
}

cmd_status() {
  check_docker
  if curl -fsS --max-time 3 "$ENDPOINT/_localstack/health" >/dev/null 2>&1; then
    info "LocalStack працює: $ENDPOINT"
    curl -fsS "$ENDPOINT/_localstack/health" | python3 -c \
      "import json,sys;d=json.load(sys.stdin);print('  edition:',d.get('edition'),'| version:',d.get('version'));print('  ec2:',d.get('services',{}).get('ec2'))"
  else
    info "LocalStack не запущено."
  fi
  echo "Контейнери-«інстанси» EC2 (${AMI_TAG}):"
  docker ps -a --filter "ancestor=${AMI_TAG}" --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  logs)   check_docker; docker logs -f --tail 100 "$CONTAINER" ;;
  *) echo "Використання: bash scripts/localstack.sh {start|status|logs|stop}"; exit 1 ;;
esac
