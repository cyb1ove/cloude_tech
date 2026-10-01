#!/bin/bash
# =============================================================================
#  Запуск ЛБ5 (варіант 3: SQS cps-robot-telemetry + DLQ) на macOS: Intel і Apple Silicon.
#  Використання:  bash run_mac.sh [--localstack] [інші параметри run_lab5.py]
#
#  Що робить скрипт:
#    1) перевіряє наявність Homebrew;
#    2) знаходить Python 3.10+ (або встановлює python@3.12 через brew);
#    3) встановлює AWS CLI v2 (brew install awscli), якщо його немає;
#    4) створює venv і встановлює requirements.txt;
#    5) перевіряє креденшали AWS (aws sts get-caller-identity);
#    6) запускає run_lab5.py з переданими аргументами.
#  Сумісно з системним bash 3.2 у macOS і працює з терміналу zsh.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

info() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. Лише macOS ------------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || info "Це не macOS ($(uname -s)), але продовжую."

# --- 1. Homebrew --------------------------------------------------------------
# На Apple Silicon brew лежить в /opt/homebrew, на Intel у /usr/local.
if ! command -v brew >/dev/null 2>&1; then
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)" && break
  done
fi
HAVE_BREW=0
command -v brew >/dev/null 2>&1 && HAVE_BREW=1

# --- 2. Python 3.10+ ----------------------------------------------------------
is_ok_python() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && is_ok_python "$(command -v "$cand")"; then
    PY="$(command -v "$cand")"; break
  fi
done
if [ -z "$PY" ]; then
  [ "$HAVE_BREW" = 1 ] || fail "Потрібен Python 3.10+. Встановіть Homebrew (https://brew.sh), потім: brew install python@3.12"
  info "Python 3.10+ не знайдено (системний /usr/bin/python3 має версію 3.9). Встановлюю python@3.12..."
  brew install python@3.12
  PY="$(brew --prefix)/bin/python3.12"
fi
info "Python: $PY ($("$PY" -V 2>&1))"

# --- 3. AWS CLI v2 ------------------------------------------------------------
if ! command -v aws >/dev/null 2>&1; then
  [ "$HAVE_BREW" = 1 ] || fail "AWS CLI не знайдено. Встановіть: brew install awscli (або pkg-інсталятор з aws.amazon.com/cli)"
  info "Встановлюю AWS CLI v2 (brew install awscli)..."
  brew install awscli
fi
info "$(aws --version 2>&1)"

# --- 4. Віртуальне середовище -------------------------------------------------
# Якщо venv створено іншим (старим) Python, наприклад системним 3.9, створюємо його заново.
if [ -x venv/bin/python ] && ! is_ok_python venv/bin/python; then
  info "Наявний venv створено старим Python — перестворюю."
  rm -rf venv
fi
if [ ! -x venv/bin/python ]; then
  info "Створюю venv..."
  "$PY" -m venv venv
fi
info "Встановлюю залежності з requirements.txt..."
venv/bin/python -m pip install --quiet --upgrade pip
venv/bin/python -m pip install --quiet -r requirements.txt certifi

# --- 5. Креденшали AWS або LocalStack ----------------------------------------
USE_LOCALSTACK=0
for a in ${@+"$@"}; do [ "$a" = "--localstack" ] && USE_LOCALSTACK=1; done

if [ "$USE_LOCALSTACK" = 1 ]; then
  # Режим емулятора: справжні ключі AWS не потрібні, потрібен запущений LocalStack
  if ! curl -fsS --max-time 3 http://localhost:4566/_localstack/health >/dev/null 2>&1; then
    fail "LocalStack не запущено. Спершу виконайте: bash scripts/localstack.sh start"
  fi
  info "LocalStack доступний на http://localhost:4566"
else
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    fail "AWS не приймає креденшали. Виконайте 'aws configure' (або вставте креденшали AWS Academy в ~/.aws/credentials, разом з aws_session_token) і повторіть. Без акаунта AWS: bash scripts/localstack.sh start && bash run_mac.sh --localstack"
  fi
  info "AWS акаунт: $(aws sts get-caller-identity --query Arn --output text)"
fi

# --- 6. Запуск ----------------------------------------------------------------
# ${@+"$@"} замість "$@": bash 3.2 з set -u падає на порожньому "$@"
info "Запускаю run_lab5.py ${*:-}"
exec venv/bin/python run_lab5.py ${@+"$@"}
