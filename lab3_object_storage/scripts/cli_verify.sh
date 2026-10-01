#!/bin/bash
# Перевірка бакета ЛБ3 засобами AWS CLI v2 (команди з методички + розширення).
#   bash scripts/cli_verify.sh               # AWS
#   bash scripts/cli_verify.sh --localstack  # LocalStack
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--localstack" ]; then
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test; unset AWS_SESSION_TOKEN AWS_PROFILE
  aws() { command aws --endpoint-url http://localhost:4566 "$@"; }
  echo "Режим: LocalStack"
fi
export AWS_DEFAULT_REGION="$(python3 -c 'import json;print(json.load(open("config/s3_config.json"))["region"])')"
B="$(python3 -c 'import json;print(json.load(open("output/s3_lifecycle_status.json"))["bucket"])')"
KEY="$(python3 -c 'import json;print(json.load(open("output/telemetry_manifest.json"))["packets"][2]["key"])')"
echo "Бакет: $B"
echo "== 1. Шифрування за замовчуванням =="; aws s3api get-bucket-encryption --bucket "$B"
echo "== 2. Правила життєвого циклу ==";    aws s3api get-bucket-lifecycle-configuration --bucket "$B"
echo "== 3. Версійність / Block Public Access / Ownership =="
aws s3api get-bucket-versioning --bucket "$B"
aws s3api get-public-access-block --bucket "$B"
aws s3api get-bucket-ownership-controls --bucket "$B" || true
aws s3api get-bucket-policy --bucket "$B" --query Policy --output text 2>/dev/null || echo "(політики немає)"
echo "== 4. Версії об'єкта seq0001 =="
aws s3api list-object-versions --bucket "$B" --prefix "$(dirname "$KEY")/device_CPS-SMART-METER-03_seq0001" \
  --query '{Versions:Versions[].{Id:VersionId,Latest:IsLatest,Size:Size},Markers:DeleteMarkers[].VersionId}' --output table
echo "== 5. Presigned GET (TTL 300 с) + curl -I =="
URL="$(aws s3 presign "s3://$B/$KEY" --expires-in 300)"
curl -sI "$URL" | grep -iE '^HTTP|x-amz-server-side-encryption|x-amz-version-id|content-length'
echo "== 6. Анонімний доступ без підпису (очікується 403) =="
if [ "${1:-}" = "--localstack" ]; then curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://localhost:4566/$B/$KEY"
else curl -s -o /dev/null -w "HTTP %{http_code}\n" "https://$B.s3.$AWS_DEFAULT_REGION.amazonaws.com/$KEY"; fi
