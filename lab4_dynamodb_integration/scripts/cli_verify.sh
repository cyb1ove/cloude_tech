#!/bin/bash
# Перевірка таблиці ЛБ4 засобами AWS CLI v2 (після: python3 run_lab4.py --keep [--localstack]).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--localstack" ]; then
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test; unset AWS_SESSION_TOKEN AWS_PROFILE
  aws() { command aws --endpoint-url http://localhost:4566 "$@"; }
  echo "Режим: LocalStack"
fi
export AWS_DEFAULT_REGION="$(python3 -c 'import json;print(json.load(open("config/dynamodb_schema.json"))["region"])')"
T="$(python3 -c 'import json;print(json.load(open("output/benchmark_report.json"))["table"]["TableName"])')"
read -r R S E <<<"$(python3 -c 'import json;w=json.load(open("output/benchmark_report.json"))["query_window"];print(w["robot"],w["start"],w["end"])')"
echo "Таблиця: $T"
echo "== 1. Схема таблиці та GSI =="
aws dynamodb describe-table --table-name "$T" --query 'Table.{Status:TableStatus,Items:ItemCount,Keys:KeySchema,Billing:BillingModeSummary.BillingMode,GSI:GlobalSecondaryIndexes[].{Name:IndexName,Keys:KeySchema,Status:IndexStatus}}'
echo "== 2. TTL =="; aws dynamodb describe-time-to-live --table-name "$T"
echo "== 3. Query: $R за останні 30 хв =="
aws dynamodb query --table-name "$T" --key-condition-expression "RobotID = :r AND #ts BETWEEN :a AND :b" \
  --expression-attribute-names '{"#ts":"Timestamp"}' \
  --expression-attribute-values "{\":r\":{\"S\":\"$R\"},\":a\":{\"N\":\"$S\"},\":b\":{\"N\":\"$E\"}}" \
  --select COUNT --return-consumed-capacity TOTAL
echo "== 4. Scan + FilterExpression OperationalState = ERROR =="
aws dynamodb scan --table-name "$T" --filter-expression "OperationalState = :s" \
  --expression-attribute-values '{":s":{"S":"ERROR"}}' --select COUNT --return-consumed-capacity TOTAL
echo "== 5. Query по GSI: ERROR і JointTorqueNm >= 13.5 =="
aws dynamodb query --table-name "$T" --index-name GSI_OperationalState_JointTorque \
  --key-condition-expression "OperationalState = :s AND JointTorqueNm >= :t" \
  --expression-attribute-values '{":s":{"S":"ERROR"},":t":{"N":"13.5"}}' --select COUNT --return-consumed-capacity TOTAL
