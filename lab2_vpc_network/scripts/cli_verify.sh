#!/bin/bash
# Незалежна перевірка мережевої інфраструктури ЛБ2 засобами AWS CLI v2.
#   bash scripts/cli_verify.sh               # AWS
#   bash scripts/cli_verify.sh --localstack  # LocalStack
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--localstack" ]; then
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
  unset AWS_SESSION_TOKEN AWS_PROFILE
  aws() { command aws --endpoint-url http://localhost:4566 "$@"; }
  echo "Режим: LocalStack (http://localhost:4566)"
fi
REGION="$(python3 -c 'import json;print(json.load(open("config/network_schema.json"))["region"])' 2>/dev/null || echo eu-central-1)"
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
[ -f output/network_state.json ] || { echo "Немає output/network_state.json — спершу розгорніть інфраструктуру."; exit 1; }
VPC="$(python3 -c 'import json;print(json.load(open("output/network_state.json"))["vpc_id"])')"
F="Name=vpc-id,Values=$VPC"
echo "Регіон: $REGION · VPC: $VPC"

echo "== VPC =="
aws ec2 describe-vpcs --vpc-ids "$VPC" \
  --query 'Vpcs[].{VPC:VpcId,CIDR:CidrBlock,State:State,Name:Tags[?Key==`Name`]|[0].Value}' --output table
echo "== Підмережі =="
aws ec2 describe-subnets --filters "$F" \
  --query 'Subnets[].{Subnet:SubnetId,CIDR:CidrBlock,AZ:AvailabilityZone,FreeIPs:AvailableIpAddressCount,PublicIP:MapPublicIpOnLaunch,Name:Tags[?Key==`Name`]|[0].Value}' --output table
echo "== Таблиці маршрутизації =="
aws ec2 describe-route-tables --filters "$F" \
  --query 'RouteTables[].Routes[].{Dest:DestinationCidrBlock,IGW:GatewayId,NAT:NatGatewayId,State:State}' --output table
echo "== Internet / NAT Gateway =="
aws ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC" \
  --query 'InternetGateways[].{IGW:InternetGatewayId,State:Attachments[0].State}' --output table
aws ec2 describe-nat-gateways --filter "$F" \
  --query 'NatGateways[].{NAT:NatGatewayId,State:State,Subnet:SubnetId,PublicIP:NatGatewayAddresses[0].PublicIp,PrivateIP:NatGatewayAddresses[0].PrivateIp}' --output table
echo "== Security Groups =="
aws ec2 describe-security-groups --filters "$F" \
  --query 'SecurityGroups[].IpPermissions[].{Proto:IpProtocol,From:FromPort,To:ToPort,CIDR:IpRanges[0].CidrIp,FromSG:UserIdGroupPairs[0].GroupId}' --output table
echo "== Network ACL =="
aws ec2 describe-network-acls --filters "$F" "Name=default,Values=false" \
  --query 'NetworkAcls[].Entries[].{Rule:RuleNumber,Egress:Egress,Proto:Protocol,From:PortRange.From,To:PortRange.To,CIDR:CidrBlock,Action:RuleAction}' --output table
echo "== Інстанси =="
aws ec2 describe-instances --filters "$F" "Name=instance-state-name,Values=pending,running" \
  --query 'Reservations[].Instances[].{ID:InstanceId,Name:Tags[?Key==`Name`]|[0].Value,PrivateIP:PrivateIpAddress,PublicIP:PublicIpAddress,Subnet:SubnetId,State:State.Name}' --output table
