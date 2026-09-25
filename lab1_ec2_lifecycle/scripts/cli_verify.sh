#!/bin/bash
# Незалежна перевірка ресурсів варіанта 3 засобами AWS CLI v2.
# Запуск (поки інстанс існує, напр. після main.py --keep):  bash scripts/cli_verify.sh
# Для емулятора LocalStack:                                   bash scripts/cli_verify.sh --localstack
set -euo pipefail
cd "$(dirname "$0")/.."

# aws() — обгортка: у режимі LocalStack додає --endpoint-url і фіктивні ключі
if [ "${1:-}" = "--localstack" ]; then
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
  unset AWS_SESSION_TOKEN AWS_PROFILE
  aws() { command aws --endpoint-url http://localhost:4566 "$@"; }
  SG_FILTER="default"
  echo "Режим: LocalStack (http://localhost:4566)"
else
  SG_FILTER=""
fi
NAME="cps-sensor-aggregator"
SG="cps-lab1-v3-mqtt-secgroup"
# Регіон: змінна середовища -> config/settings.json -> профіль aws configure
REGION="${AWS_REGION:-$(python3 -c 'import json;print(json.load(open("config/settings.json"))["region_name"])' 2>/dev/null || aws configure get region || echo eu-central-1)}"
echo "Регіон: $REGION"
# Регіон за замовчуванням для всіх викликів (sts тощо), навіть якщо aws configure не виконувався
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"

echo "== Хто я (перевірка креденшалів) =="
aws sts get-caller-identity --output table

echo "== Інстанс ${NAME} =="
aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=${NAME}" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,State:State.Name,IP:PublicIpAddress,AZ:Placement.AvailabilityZone,AMI:ImageId}' \
  --output table

echo "== Кореневий том EBS =="
aws ec2 describe-volumes --region "$REGION" \
  --filters "Name=tag:Name,Values=${NAME}" \
  --query 'Volumes[].{ID:VolumeId,Size:Size,Type:VolumeType,IOPS:Iops,Throughput:Throughput,State:State,Encrypted:Encrypted}' \
  --output table

echo "== Правила Security Group ${SG} =="
aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=${SG_FILTER:-$SG}" \
  --query 'SecurityGroups[].IpPermissions[].{Proto:IpProtocol,From:FromPort,To:ToPort,CIDR:IpRanges[0].CidrIp,Desc:IpRanges[0].Description}' \
  --output table

echo "== Характеристики t3.micro =="
aws ec2 describe-instance-types --region "$REGION" --instance-types t3.micro \
  --query 'InstanceTypes[].{vCPU:VCpuInfo.DefaultVCpus,RAM_MiB:MemoryInfo.SizeInMiB,Net:NetworkInfo.NetworkPerformance,EBS_Mbps:EbsInfo.EbsOptimizedInfo.BaselineBandwidthInMbps}' \
  --output table

# Перевірка брокера з локальної машини (потрібен пакет mosquitto-clients):
#   IP=$(jq -r .PublicIpAddress output/instance_manifest.json)
#   USER=$(jq -r .username output/mqtt_credentials.json); PASS=$(jq -r .password output/mqtt_credentials.json)
#   mosquitto_sub -h "$IP" -p 1883 -u "$USER" -P "$PASS" -t 'cps/#' -v -C 1
#   mosquitto_pub -h "$IP" -p 1883 -u "$USER" -P "$PASS" -t cps/sensors/t1 -m '{"temp":21.4}'
# Лог cloud-init на інстансі:
#   ssh -i cps-lab1-v3-keypair.pem admin@$IP 'sudo
