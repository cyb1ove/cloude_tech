"""
Модуль побудови ізольованої мережевої інфраструктури VPC з Bastion Host (Boto3).

ЛБ2 · Варіант 3: VPC 10.30.0.0/16
    публічна підмережа  10.30.2.0/24  (eu-central-1a) — Bastion Host, NAT Gateway
    приватна підмережа  10.30.50.0/24 (eu-central-1a) — OPC UA Industrial Server, TCP 4840

Порівняно з еталонним прикладом додано:
    * автоматичний пошук AMI Ubuntu 22.04 (офіційний власник Canonical 099720109477);
    * пару ключів генерує локально ssh-keygen (ed25519), в AWS імпортується лише публічний ключ;
    * SSH до бастіону дозволено лише з IP розробника (/32), а не з 0.0.0.0/0;
    * другий рівень захисту — власні Network ACL для обох підмереж (stateless, з ефемерними портами);
    * фіксовані приватні IP вузлів (10.30.2.10, 10.30.50.20) для відтворюваності тестів;
    * вимірювання тривалості етапів (зокрема очікування NAT Gateway);
    * повне видалення інфраструктури у правильному порядку залежностей (destroy);
    * режим LocalStack (змінюється лише endpoint API).
"""

import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, WaiterError

HERE = os.path.dirname(os.path.abspath(__file__))


def log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


class VPCNetworkBuilder:
    """Створення, аудит і видалення багаторівневої топології VPC + Bastion Host."""

    def __init__(self, config_path: str, localstack: bool = False):
        with open(config_path, "r", encoding="utf-8") as f:
            self.cfg: Dict[str, Any] = json.load(f)

        self.region = self.cfg.get("region", "eu-central-1")
        self.localstack = localstack or os.environ.get("CPS_LOCALSTACK") == "1"
        if self.localstack:
            endpoint = os.environ.get("LOCALSTACK_ENDPOINT",
                                      self.cfg.get("localstack", {}).get("endpoint_url", "http://localhost:4566"))
            session = boto3.Session(region_name=self.region,
                                    aws_access_key_id="test", aws_secret_access_key="test")
            self.ec2 = session.client("ec2", endpoint_url=endpoint)
            log("INFO", f"Режим LocalStack: API EC2 -> {endpoint} (регіон {self.region})")
        else:
            session = boto3.Session(region_name=self.region)
            self.ec2 = session.client("ec2")

        self.key_name = self.cfg.get("key_name", "cps-lab2-key")
        self.key_path = os.path.abspath(f"{self.key_name}.pem")
        self.service_port = int(self.cfg["service"]["port"])
        self.state: Dict[str, Any] = {
            "variant": self.cfg.get("variant"),
            "region": self.region,
            "backend": "LocalStack" if self.localstack else "AWS",
            "vpc_cidr": self.cfg["vpc_cidr"],
            "public_subnet_cidr": self.cfg["public_subnet"]["cidr"],
            "private_subnet_cidr": self.cfg["private_subnet"]["cidr"],
            "service_port": self.service_port,
            "timings_s": {},
        }

    # ------------------------------------------------------------ утиліти
    def _tags(self, name: str) -> List[Dict[str, str]]:
        tags = [{"Key": "Name", "Value": name}]
        tags += [{"Key": k, "Value": v} for k, v in self.cfg.get("tags", {}).items()]
        return tags

    def _spec(self, resource_type: str, name: str) -> List[Dict[str, Any]]:
        return [{"ResourceType": resource_type, "Tags": self._tags(name)}]

    def _timed_wait(self, label: str, waiter_name: str, delay: int = 15,
                    max_attempts: int = 40, **kwargs: Any) -> float:
        started = time.time()
        waiter = self.ec2.get_waiter(waiter_name)
        try:
            waiter.wait(WaiterConfig={"Delay": delay, "MaxAttempts": max_attempts}, **kwargs)
        except WaiterError as err:
            raise RuntimeError(f"Очікувач {waiter_name} вичерпав ліміт: {err}") from err
        elapsed = round(time.time() - started, 1)
        self.state["timings_s"][label] = elapsed
        return elapsed

    @staticmethod
    def _detect_public_ip_cidr() -> str:
        """IP розробника для правила SSH (/32); запасний варіант — 0.0.0.0/0."""
        url, ip = "https://checkip.amazonaws.com", None
        try:
            ctx = ssl.create_default_context()
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                pass
            with urllib.request.urlopen(url, timeout=5, context=ctx) as resp:
                ip = resp.read().decode().strip()
        except Exception:  # noqa: BLE001
            curl = shutil.which("curl")
            if curl:
                try:
                    ip = subprocess.run([curl, "-fsS", "--max-time", "5", url], capture_output=True,
                                        text=True, check=True).stdout.strip()
                except (subprocess.SubprocessError, OSError):
                    ip = None
        try:
            socket.inet_aton(ip or "")
            return f"{ip}/32"
        except OSError:
            log("WARN", "Не вдалося визначити ваш IP; SSH до бастіону буде відкрито для 0.0.0.0/0.")
            return "0.0.0.0/0"

    # --------------------------------------------------------------- AMI
    def resolve_ami(self) -> str:
        configured = self.cfg.get("ami_id", "auto")
        if configured and configured != "auto":
            self.state["ami_id"] = configured
            return configured
        lookup = self.cfg.get("ami_lookup", {})
        log("INFO", "Пошук актуального AMI Ubuntu 22.04 LTS (Canonical)...")
        images = self.ec2.describe_images(
            Owners=[lookup.get("owner_id", "099720109477")],
            Filters=[{"Name": "name", "Values": [lookup.get("name_pattern", "ubuntu/images/hvm-ssd*/ubuntu-jammy-22.04-amd64-server-*")]},
                     {"Name": "architecture", "Values": ["x86_64"]},
                     {"Name": "state", "Values": ["available"]}],
        ).get("Images", [])
        if not images and self.localstack:
            # Емулятор має обмежений каталог образів: беремо будь-який доступний
            images = self.ec2.describe_images().get("Images", [])
        if not images:
            raise RuntimeError("Не знайдено AMI Ubuntu 22.04 у регіоні " + self.region)
        image = sorted(images, key=lambda i: i.get("CreationDate", ""), reverse=True)[0]
        self.state["ami_id"] = image["ImageId"]
        log("SUCCESS", f"Обрано AMI {image['ImageId']} ({image.get('Name', 'n/a')}).")
        return image["ImageId"]

    # ----------------------------------------------------------- SSH-ключ
    def create_key_pair(self) -> str:
        """Ключ генерується локально (ed25519); в AWS імпортується лише публічна частина."""
        try:
            self.ec2.delete_key_pair(KeyName=self.key_name)
        except ClientError:
            pass
        for path in (self.key_path, self.key_path + ".pub"):
            if os.path.exists(path):
                os.chmod(path, 0o600)
                os.remove(path)

        ssh_keygen = shutil.which("ssh-keygen")
        if ssh_keygen:
            subprocess.run([ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", self.key_name,
                            "-f", self.key_path], check=True)
            with open(self.key_path + ".pub", "rb") as f:
                self.ec2.import_key_pair(KeyName=self.key_name, PublicKeyMaterial=f.read(),
                                         TagSpecifications=self._spec("key-pair", self.key_name))
            log("SUCCESS", f"Згенеровано SSH-ключ ed25519: {os.path.basename(self.key_path)} "
                           "(в AWS передано лише публічний ключ)")
        else:
            material = self.ec2.create_key_pair(KeyName=self.key_name, KeyType="ed25519")["KeyMaterial"]
            with open(self.key_path, "w", encoding="utf-8") as f:
                f.write(material)
            log("SUCCESS", f"Згенеровано SSH-ключ: {os.path.basename(self.key_path)}")
        os.chmod(self.key_path, 0o400)
        self.state["key_name"] = self.key_name
        self.state["key_path"] = self.key_path
        return self.key_name

    # ---------------------------------------------------- мережева топологія
    def build_network_topology(self) -> None:
        vpc_cidr = self.cfg["vpc_cidr"]
        pub, priv = self.cfg["public_subnet"], self.cfg["private_subnet"]

        # 1. VPC + DNS
        log("INFO", f"Створення VPC з CIDR {vpc_cidr}...")
        vpc_id = self.ec2.create_vpc(CidrBlock=vpc_cidr,
                                     TagSpecifications=self._spec("vpc", self.cfg["vpc_name"]))["Vpc"]["VpcId"]
        self.state["vpc_id"] = vpc_id
        self._timed_wait("vpc_available", "vpc_available", delay=2, VpcIds=[vpc_id])
        # DNS-імена потрібні для роботи сервісів AWS і зручного доступу за іменем
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        log("SUCCESS", f"VPC створено. ID: {vpc_id}")

        # 2. Internet Gateway
        log("INFO", "Створення та приєднання Internet Gateway...")
        igw_id = self.ec2.create_internet_gateway(
            TagSpecifications=self._spec("internet-gateway", f"{self.cfg['vpc_name']}-igw")
        )["InternetGateway"]["InternetGatewayId"]
        self.state["igw_id"] = igw_id
        self.ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        log("SUCCESS", f"IGW створено та приєднано. ID: {igw_id}")

        # 3. Підмережі
        log("INFO", f"Створення публічної підмережі {pub['cidr']} у зоні {pub['az']}...")
        pub_id = self.ec2.create_subnet(VpcId=vpc_id, CidrBlock=pub["cidr"], AvailabilityZone=pub["az"],
                                        TagSpecifications=self._spec("subnet", pub["name"]))["Subnet"]["SubnetId"]
        self.state["public_subnet_id"] = pub_id
        # Інстанси публічної підмережі автоматично отримують публічну IPv4
        self.ec2.modify_subnet_attribute(SubnetId=pub_id, MapPublicIpOnLaunch={"Value": True})

        log("INFO", f"Створення приватної підмережі {priv['cidr']} у зоні {priv['az']}...")
        priv_id = self.ec2.create_subnet(VpcId=vpc_id, CidrBlock=priv["cidr"], AvailabilityZone=priv["az"],
                                         TagSpecifications=self._spec("subnet", priv["name"]))["Subnet"]["SubnetId"]
        self.state["private_subnet_id"] = priv_id
        self._timed_wait("subnets_available", "subnet_available", delay=2, SubnetIds=[pub_id, priv_id])

        # 4. Elastic IP + NAT Gateway у ПУБЛІЧНІЙ підмережі
        log("INFO", "Виділення Elastic IP для NAT Gateway...")
        eip = self.ec2.allocate_address(Domain="vpc",
                                        TagSpecifications=self._spec("elastic-ip", f"{self.cfg['vpc_name']}-nat-eip"))
        self.state["nat_eip_allocation_id"] = eip["AllocationId"]
        self.state["nat_public_ip"] = eip["PublicIp"]

        log("INFO", "Створення NAT Gateway (зазвичай 1–3 хв)...")
        nat_id = self.ec2.create_nat_gateway(
            SubnetId=pub_id, AllocationId=eip["AllocationId"], ConnectivityType="public",
            TagSpecifications=self._spec("natgateway", f"{self.cfg['vpc_name']}-nat"),
        )["NatGateway"]["NatGatewayId"]
        self.state["nat_gateway_id"] = nat_id
        waited = self._timed_wait("nat_gateway_available", "nat_gateway_available", NatGatewayIds=[nat_id])
        nat = self.ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]
        addr = (nat.get("NatGatewayAddresses") or [{}])[0]
        self.state["nat_private_ip"] = addr.get("PrivateIp")
        self.state["nat_public_ip"] = addr.get("PublicIp") or self.state["nat_public_ip"]
        log("SUCCESS", f"NAT готовий за {waited} с. Public IP: {self.state['nat_public_ip']}, "
                       f"Private IP: {self.state['nat_private_ip']}")

        # 5. Таблиці маршрутизації
        log("INFO", "Налаштування таблиці маршрутизації для публічної підмережі (0.0.0.0/0 -> IGW)...")
        pub_rt = self.ec2.create_route_table(
            VpcId=vpc_id, TagSpecifications=self._spec("route-table", f"{self.cfg['vpc_name']}-public-rt")
        )["RouteTable"]["RouteTableId"]
        self.ec2.create_route(RouteTableId=pub_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        pub_assoc = self.ec2.associate_route_table(RouteTableId=pub_rt, SubnetId=pub_id)["AssociationId"]

        log("INFO", "Налаштування таблиці маршрутизації для приватної підмережі (0.0.0.0/0 -> NAT)...")
        priv_rt = self.ec2.create_route_table(
            VpcId=vpc_id, TagSpecifications=self._spec("route-table", f"{self.cfg['vpc_name']}-private-rt")
        )["RouteTable"]["RouteTableId"]
        self.ec2.create_route(RouteTableId=priv_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)
        priv_assoc = self.ec2.associate_route_table(RouteTableId=priv_rt, SubnetId=priv_id)["AssociationId"]
        self.state.update({"public_route_table_id": pub_rt, "private_route_table_id": priv_rt,
                           "route_table_associations": [pub_assoc, priv_assoc]})
        log("SUCCESS", "Маршрутизацію налаштовано.")

    # ------------------------------------------------------- Network ACL
    def _nacl_entry(self, acl_id: str, rule: int, egress: bool, proto: str, cidr: str,
                    ports: Optional[tuple] = None) -> None:
        params: Dict[str, Any] = {"NetworkAclId": acl_id, "RuleNumber": rule, "Egress": egress,
                                  "Protocol": proto, "RuleAction": "allow", "CidrBlock": cidr}
        if ports:
            params["PortRange"] = {"From": ports[0], "To": ports[1]}
        if proto == "1":
            params["IcmpTypeCode"] = {"Type": -1, "Code": -1}
        self.ec2.create_network_acl_entry(**params)

    def _associate_nacl(self, acl_id: str, subnet_id: str) -> None:
        acls = self.ec2.describe_network_acls(
            Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}])["NetworkAcls"]
        for acl in acls:
            for assoc in acl.get("Associations", []):
                if assoc.get("SubnetId") == subnet_id:
                    self.ec2.replace_network_acl_association(AssociationId=assoc["NetworkAclAssociationId"],
                                                             NetworkAclId=acl_id)
                    return
        raise RuntimeError(f"Не знайдено асоціацію NACL для {subnet_id}")

    def setup_network_acls(self) -> None:
        """Другий (stateless) рівень захисту на межі підмереж.

        NACL не відстежують з'єднання, тому для відповідей потрібні явні правила
        на ефемерні порти 1024–65535. Усе, що не дозволено, блокує правило '*'.
        """
        vpc_id, vpc_cidr = self.state["vpc_id"], self.cfg["vpc_cidr"]
        pub_cidr = self.cfg["public_subnet"]["cidr"]
        ssh_cidr = self.state["ssh_ingress_cidr"]

        log("INFO", "Створення Network ACL для публічної підмережі...")
        pub_acl = self.ec2.create_network_acl(
            VpcId=vpc_id, TagSpecifications=self._spec("network-acl", f"{self.cfg['vpc_name']}-public-nacl")
        )["NetworkAcl"]["NetworkAclId"]
        self._nacl_entry(pub_acl, 100, False, "6", ssh_cidr, (22, 22))            # SSH до бастіону
        self._nacl_entry(pub_acl, 110, False, "-1", vpc_cidr)                    # трафік з VPC (у т.ч. до NAT)
        self._nacl_entry(pub_acl, 120, False, "6", "0.0.0.0/0", (1024, 65535))   # відповіді TCP з Інтернету
        self._nacl_entry(pub_acl, 130, False, "17", "0.0.0.0/0", (1024, 65535))  # відповіді UDP (NTP тощо)
        self._nacl_entry(pub_acl, 140, False, "1", "0.0.0.0/0")                  # ICMP (traceroute/PMTUD)
        self._nacl_entry(pub_acl, 100, True, "-1", "0.0.0.0/0")                  # вихідний трафік
        self._associate_nacl(pub_acl, self.state["public_subnet_id"])

        log("INFO", "Створення Network ACL для приватної підмережі...")
        priv_acl = self.ec2.create_network_acl(
            VpcId=vpc_id, TagSpecifications=self._spec("network-acl", f"{self.cfg['vpc_name']}-private-nacl")
        )["NetworkAcl"]["NetworkAclId"]
        self._nacl_entry(priv_acl, 100, False, "6", pub_cidr, (22, 22))                              # SSH лише з публічної підмережі
        self._nacl_entry(priv_acl, 110, False, "6", pub_cidr, (self.service_port, self.service_port))  # OPC UA
        self._nacl_entry(priv_acl, 120, False, "6", "0.0.0.0/0", (1024, 65535))  # відповіді через NAT
        self._nacl_entry(priv_acl, 130, False, "17", "0.0.0.0/0", (1024, 65535))
        self._nacl_entry(priv_acl, 140, False, "1", vpc_cidr)                    # ICMP лише з VPC
        self._nacl_entry(priv_acl, 150, False, "1", "0.0.0.0/0")                 # ICMP time-exceeded для traceroute
        self._nacl_entry(priv_acl, 100, True, "-1", "0.0.0.0/0")
        self._associate_nacl(priv_acl, self.state["private_subnet_id"])

        self.state.update({"public_nacl_id": pub_acl, "private_nacl_id": priv_acl})
        log("SUCCESS", f"NACL: public {pub_acl}, private {priv_acl}.")

    # ---------------------------------------------------- Security Groups
    def setup_security_groups(self) -> None:
        vpc_id = self.state["vpc_id"]
        ssh_cidr = self.state["ssh_ingress_cidr"]

        log("INFO", "Створення Security Group для Bastion Host...")
        bastion_sg = self.ec2.create_security_group(
            GroupName="cps-v3-bastion-sg", Description="Bastion: SSH from developer IP only", VpcId=vpc_id,
            TagSpecifications=self._spec("security-group", "cps-v3-bastion-sg"))["GroupId"]
        self.ec2.authorize_security_group_ingress(GroupId=bastion_sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH from developer"}]},
        ])

        log("INFO", "Створення Security Group для Private Worker (OPC UA)...")
        worker_sg = self.ec2.create_security_group(
            GroupName="cps-v3-opcua-worker-sg", Description="OPC UA worker: SSH/4840/ICMP from bastion SG only",
            VpcId=vpc_id, TagSpecifications=self._spec("security-group", "cps-v3-opcua-worker-sg"))["GroupId"]
        from_bastion = [{"GroupId": bastion_sg, "Description": "from Bastion SG"}]
        self.ec2.authorize_security_group_ingress(GroupId=worker_sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "UserIdGroupPairs": from_bastion},
            {"IpProtocol": "tcp", "FromPort": self.service_port, "ToPort": self.service_port,
             "UserIdGroupPairs": from_bastion},
            {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1, "UserIdGroupPairs": from_bastion},
        ])
        self.state.update({"bastion_sg_id": bastion_sg, "worker_sg_id": worker_sg})
        log("SUCCESS", f"Групи безпеки успішно налаштовані ({bastion_sg}, {worker_sg}).")

    # --------------------------------------------------------- інстанси
    def _render_worker_user_data(self) -> str:
        svc = self.cfg["service"]
        with open(os.path.join(HERE, "worker_user_data.sh"), encoding="utf-8") as f:
            tpl = f.read()
        with open(os.path.join(HERE, "opcua_server.py"), encoding="utf-8") as f:
            server_py = f.read()
        data = (tpl.replace("__OPCUA_SERVER_PY__", server_py)
                   .replace("__OPCUA_PORT__", str(svc["port"]))
                   .replace("__OPCUA_PATH__", svc["endpoint_path"])
                   .replace("__OPCUA_NS__", svc["namespace_uri"]))
        if len(data.encode()) > 16 * 1024:
            raise ValueError("User Data перевищує 16 КБ")
        return data

    def launch_instances(self) -> None:
        ami = self.state["ami_id"]
        itype = self.cfg["instance_type"]
        with open(os.path.join(HERE, "bastion_user_data.sh"), encoding="utf-8") as f:
            bastion_ud = f.read()
        common = {"ImageId": ami, "InstanceType": itype, "KeyName": self.key_name, "MinCount": 1, "MaxCount": 1,
                  "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"}}

        log("INFO", "Запуск інстансу Bastion Host у публічній підмережі...")
        bastion = self.ec2.run_instances(
            **common, SubnetId=self.state["public_subnet_id"], PrivateIpAddress=self.cfg["bastion"]["private_ip"],
            SecurityGroupIds=[self.state["bastion_sg_id"]], UserData=bastion_ud,
            TagSpecifications=self._spec("instance", self.cfg["bastion"]["name"]) + self._spec("volume", self.cfg["bastion"]["name"]),
        )["Instances"][0]["InstanceId"]

        log("INFO", "Запуск інстансу Private Worker (OPC UA) у приватній підмережі...")
        worker = self.ec2.run_instances(
            **common, SubnetId=self.state["private_subnet_id"], PrivateIpAddress=self.cfg["worker"]["private_ip"],
            SecurityGroupIds=[self.state["worker_sg_id"]], UserData=self._render_worker_user_data(),
            TagSpecifications=self._spec("instance", self.cfg["worker"]["name"]) + self._spec("volume", self.cfg["worker"]["name"]),
        )["Instances"][0]["InstanceId"]
        self.state.update({"bastion_instance_id": bastion, "worker_instance_id": worker})

        log("INFO", "Очікування переходу обох інстансів у стан 'running'...")
        waited = self._timed_wait("instances_running", "instance_running", InstanceIds=[bastion, worker])
        res = self.ec2.describe_instances(InstanceIds=[bastion, worker])
        by_id = {i["InstanceId"]: i for r in res["Reservations"] for i in r["Instances"]}
        b, w = by_id[bastion], by_id[worker]
        self.state.update({
            "bastion_public_ip": b.get("PublicIpAddress"),
            "bastion_private_ip": b.get("PrivateIpAddress"),
            "worker_private_ip": w.get("PrivateIpAddress"),
            "worker_public_ip": w.get("PublicIpAddress"),
        })
        log("SUCCESS", f"Інстанси running за {waited} с.")
        log("SUCCESS", f"Bastion Host: Public IP = {b.get('PublicIpAddress')}, Private IP = {b.get('PrivateIpAddress')}")
        log("SUCCESS", f"Private Worker: Private IP = {w.get('PrivateIpAddress')}, "
                       f"Public IP = {w.get('PublicIpAddress') or 'None (Secure Isolated)'}")

    # --------------------------------------------------------- повний цикл
    def deploy(self, enable_nacl: bool = True) -> Dict[str, Any]:
        started = time.time()
        ssh_cidr = self.cfg.get("ssh_ingress_cidr", "auto")
        self.state["ssh_ingress_cidr"] = self._detect_public_ip_cidr() if ssh_cidr == "auto" else ssh_cidr
        self.resolve_ami()
        self.build_network_topology()
        self.create_key_pair()
        if enable_nacl:
            self.setup_network_acls()
        self.setup_security_groups()
        self.launch_instances()
        self.state["timings_s"]["total_deploy"] = round(time.time() - started, 1)
        self.state["hourly_cost_usd"] = self.hourly_cost()
        return self.state

    def hourly_cost(self) -> Dict[str, float]:
        p = self.cfg.get("pricing", {})
        inst = 2 * p.get("instance_usd_per_hour", 0.012)
        nat = p.get("nat_usd_per_hour", 0.052)
        ipv4 = 2 * p.get("public_ipv4_usd_per_hour", 0.005)  # EIP NAT + публічна IP бастіону
        total = inst + nat + ipv4
        return {"instances": round(inst, 4), "nat_gateway": round(nat, 4), "public_ipv4": round(ipv4, 4),
                "total_per_hour": round(total, 4), "total_per_month_730h": round(total * 730, 2),
                "nat_per_gb": p.get("nat_usd_per_gb", 0.052)}

    # ------------------------------------------------------------ видалення
    def _try(self, what: str, fn, *args, retries: int = 1, delay: int = 10, **kwargs) -> bool:
        for attempt in range(1, retries + 1):
            try:
                fn(*args, **kwargs)
                log("SUCCESS", what)
                return True
            except ClientError as err:
                code = err.response["Error"]["Code"]
                if code.endswith("NotFound") or code in ("InvalidAllocationID.NotFound",):
                    log("INFO", f"{what}: вже відсутній")
                    return True
                if attempt < retries and code in ("DependencyViolation", "InvalidParameterValue", "IncorrectState"):
                    time.sleep(delay)
                    continue
                log("WARN", f"{what}: {code}")
                return False
        return False

    def destroy(self, st: Dict[str, Any]) -> None:
        """Видалення у зворотному порядку залежностей."""
        ec2 = self.ec2
        ids = [st.get("bastion_instance_id"), st.get("worker_instance_id")]
        ids = [i for i in ids if i]
        if ids:
            log("INFO", f"Термінація інстансів {ids}...")
            self._try("Інстанси термінуються", ec2.terminate_instances, InstanceIds=ids)
            try:
                self._timed_wait("instances_terminated", "instance_terminated", InstanceIds=ids)
            except RuntimeError as err:
                log("WARN", str(err))

        if st.get("nat_gateway_id"):
            log("INFO", "Видалення NAT Gateway (1–3 хв)...")
            self._try("NAT Gateway видаляється", ec2.delete_nat_gateway, NatGatewayId=st["nat_gateway_id"])
            for _ in range(40):
                try:
                    state = ec2.describe_nat_gateways(NatGatewayIds=[st["nat_gateway_id"]])["NatGateways"][0]["State"]
                except (ClientError, IndexError):
                    break
                if state == "deleted":
                    break
                time.sleep(10)
        if st.get("nat_eip_allocation_id"):
            self._try("Elastic IP звільнено", ec2.release_address, AllocationId=st["nat_eip_allocation_id"],
                      retries=6)

        for key in ("worker_sg_id", "bastion_sg_id"):  # спершу worker: він посилається на bastion SG
            if st.get(key):
                self._try(f"Security Group {st[key]} видалено", ec2.delete_security_group,
                          GroupId=st[key], retries=12)

        for subnet_key, acl_key in (("public_subnet_id", "public_nacl_id"), ("private_subnet_id", "private_nacl_id")):
            if st.get(acl_key) and st.get(subnet_key) and st.get("vpc_id"):
                try:
                    default = ec2.describe_network_acls(Filters=[
                        {"Name": "vpc-id", "Values": [st["vpc_id"]]}, {"Name": "default", "Values": ["true"]}]
                    )["NetworkAcls"][0]["NetworkAclId"]
                    self._associate_nacl(default, st[subnet_key])
                except (ClientError, IndexError, RuntimeError):
                    pass
                self._try(f"NACL {st[acl_key]} видалено", ec2.delete_network_acl, NetworkAclId=st[acl_key])

        for assoc in st.get("route_table_associations", []):
            self._try(f"Асоціацію {assoc} знято", ec2.disassociate_route_table, AssociationId=assoc)
        for key in ("public_route_table_id", "private_route_table_id"):
            if st.get(key):
                self._try(f"Таблицю маршрутів {st[key]} видалено", ec2.delete_route_table, RouteTableId=st[key])

        for key in ("public_subnet_id", "private_subnet_id"):
            if st.get(key):
                self._try(f"Підмережу {st[key]} видалено", ec2.delete_subnet, SubnetId=st[key], retries=6)

        if st.get("igw_id") and st.get("vpc_id"):
            self._try("IGW від'єднано", ec2.detach_internet_gateway, InternetGatewayId=st["igw_id"], VpcId=st["vpc_id"])
            self._try("IGW видалено", ec2.delete_internet_gateway, InternetGatewayId=st["igw_id"])
        if st.get("vpc_id"):
            self._try(f"VPC {st['vpc_id']} видалено", ec2.delete_vpc, VpcId=st["vpc_id"], retries=6)
        if st.get("key_name"):
            self._try(f"Пару ключів {st['key_name']} видалено з AWS", ec2.delete_key_pair, KeyName=st["key_name"])

    # ---------------------------------------------------------- аудит (для звіту)
    def describe_for_report(self, vpc_id: str) -> Dict[str, Any]:
        """Фактичний стан маршрутів, SG і NACL з API (для аналізу LPM та аудиту)."""
        rts = self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
        sgs = self.ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
        acls = self.ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NetworkAcls"]
        return {"route_tables": rts, "security_groups": sgs, "network_acls": acls}


def load_state(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cidr_inside(child: str, parent: str) -> bool:
    return ipaddress.ip_network(child).subnet_of(ipaddress.ip_network(parent))
