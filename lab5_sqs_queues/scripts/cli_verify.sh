#!/bin/bash
# Перевірка черг ЛБ5 засобами AWS CLI v2 (після: python3 run_lab5.py --keep [--localstack]).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--localstack" ]; then
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test; unset AWS_SESSION_TOKEN AWS_PROFILE
  aws() { command aws --endpoint-url http://localhost:4566 "$@"; }
  echo "Режим: LocalStack"
fi
export AWS_DEFAULT_REGION="$(python3 -c 'import json;print(json.load(open("config/sqs_config.json"))["region"])')"
read -r MAIN DLQ <<<"$(python3 -c 'import json;q=json.load(open("output/queue_benchmark_report.json"))["queues"];print(q["main_url"],q["dlq_url"])')"
echo "== 1. Черги з префіксом cps-robot-telemetry =="; aws sqs list-queues --queue-name-prefix cps-robot-telemetry
echo "== 2. Атрибути основної черги (VisibilityTimeout, RedrivePolicy) =="
aws sqs get-queue-attributes --queue-url "$MAIN" --attribute-names VisibilityTimeout MessageRetentionPeriod RedrivePolicy ReceiveMessageWaitTimeSeconds ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
echo "== 3. Кількість повідомлень у DLQ =="
aws sqs get-queue-attributes --queue-url "$DLQ" --attribute-names ApproximateNumberOfMessages RedriveAllowPolicy MessageRetentionPeriod
echo "== 4. Приклад повідомлення з DLQ (перегляд без видалення: visibility 0) =="
aws sqs receive-message --queue-url "$DLQ" --max-number-of-messages 1 --visibility-timeout 0 \
  --attribute-names ApproximateReceiveCount --message-attribute-names All \
  --query 'Messages[0].{Body:Body,ReceiveCount:Attributes.ApproximateReceiveCount,Corrupted:MessageAttributes.IsCorrupted.StringValue}'
echo "== 5. Черги-джерела для DLQ =="; aws sqs list-dead-letter-source-queues --queue-url "$DLQ" || true
