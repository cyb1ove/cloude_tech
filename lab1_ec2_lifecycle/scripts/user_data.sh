#!/bin/bash
# =============================================================================
#  User Data (cloud-init) для вузла КФС "cps-sensor-aggregator" — Варіант 3
#  ОС: Debian 12 (bookworm) | Сервіс: Eclipse Mosquitto MQTT Broker, порт 1883
#
#  Плейсхолдери __MQTT_PORT__, __MQTT_USER__, __MQTT_PASSWORD__ підставляє
#  scripts/ec2_manager.py перед відправкою в run_instances().
#  Скрипт виконується один раз, від root, на першому завантаженні ОС.
# =============================================================================
set -euxo pipefail
exec > >(tee -a /var/log/cps_user_data.log) 2>&1

MQTT_PORT="__MQTT_PORT__"
MQTT_USER="__MQTT_USER__"
MQTT_PASSWORD="__MQTT_PASSWORD__"

export DEBIAN_FRONTEND=noninteractive

# 1. Оновлення індексу пакетів та встановлення брокера й клієнтських утиліт
apt-get update -y
apt-get install -y --no-install-recommends mosquitto mosquitto-clients ca-certificates

# 2. Облікові дані сенсорів (файл паролів зберігається у вигляді хешів)
mosquitto_passwd -b -c /etc/mosquitto/passwd "${MQTT_USER}" "${MQTT_PASSWORD}"
chown mosquitto:mosquitto /etc/mosquitto/passwd
chmod 0640 /etc/mosquitto/passwd

# 3. Конфігурація брокера.
#    Mosquitto 2.x за замовчуванням слухає лише localhost, тому зовнішній
#    listener на 0.0.0.0:1883 потрібно оголосити явно.
cat > /etc/mosquitto/conf.d/cps-sensor-aggregator.conf <<EOF
# --- CPS Sensor Aggregator (Lab 1, variant 3) ---
per_listener_settings false

listener ${MQTT_PORT} 0.0.0.0
protocol mqtt

allow_anonymous false
password_file /etc/mosquitto/passwd

# Обмеження для слабких сенсорних вузлів
max_connections 500
max_inflight_messages 20
max_queued_messages 1000
message_size_limit 262144

# Персистентність: retained-повідомлення та сесії переживають stop/start.
# persistence_location і pid_file вже задані в /etc/mosquitto/mosquitto.conf
# пакета Debian; повторне оголошення рядкової опції Mosquitto 2.x вважає
# помилкою ("Duplicate ... value") і не стартує — тому тут їх немає.
persistence true
autosave_interval 60

log_type error
log_type warning
log_type notice
log_type information
connection_messages true
log_timestamp true
EOF

# 4. Автозапуск сервісу (важливо для сценарію stop -> start)
if [ -d /run/systemd/system ]; then
  # Справжня ВМ в AWS: керуємо сервісом через systemd
  systemctl enable mosquitto
  systemctl restart mosquitto
else
  # LocalStack: «інстанс» є Docker-контейнером без systemd — запускаємо демон напряму.
  # Stop/Start у LocalStack ставить контейнер на паузу, тож процес переживає цикл.
  mkdir -p /run/mosquitto /var/log/mosquitto /var/lib/mosquitto
  chown mosquitto:mosquitto /run/mosquitto /var/log/mosquitto /var/lib/mosquitto
  mosquitto -d -c /etc/mosquitto/mosquitto.conf
fi

# 5. Відкриття порту на рівні ОС.
#    В образі Debian 12 для AWS немає активного брандмауера, тому достатньо
#    правила в Security Group. Якщо ufw все ж встановлено — відкриваємо порт і тут.
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${MQTT_PORT}/tcp" || true
fi

# 6. Самоперевірка: брокер слухає порт і приймає автентифікованого клієнта
sleep 2
if command -v ss >/dev/null 2>&1; then
  ss -ltnp | grep ":${MQTT_PORT} "
fi
mosquitto_pub -h 127.0.0.1 -p "${MQTT_PORT}" -u "${MQTT_USER}" -P "${MQTT_PASSWORD}" \
  -t "cps/sensor-aggregator/status" -r -q 1 \
  -m "{\"node\":\"cps-sensor-aggregator\",\"status\":\"ONLINE\",\"boot\":\"$(date -Is)\"}"

echo "CPS_SENSOR_AGGREGATOR_READY $(date -Is)" > /var/log/cps_ready.marker
