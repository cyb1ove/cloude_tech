"""
Модуль програмного керування життєвим циклом інстансу AWS EC2 (Boto3).

Варіант 3: вузол КФС "cps-sensor-aggregator"
    t3.micro · Debian 12 · EBS gp3 15 ГБ · Mosquitto MQTT Broker, TCP 1883

Порівняно з еталонним прикладом додано:
    * автоматичний пошук актуального AMI Debian 12 (офіційний власник 136693071363);
    * коректне ім'я кореневого пристрою (/dev/xvda у Debian, а не /dev/sda1);
    * Security Group з портами 22 (лише з IP студента) та 1883 (MQTT);
    * шаблонізацію User Data з генерацією пароля MQTT;
    * обов'язковий IMDSv2 та теги для томів EBS;
    * журнал переходів станів із таймінгами (для аналізу T_wait і розрахунку C_total);
    * функціональну перевірку брокера протоколом MQTT 3.1.1 (CONNECT -> CONNACK).
"""

import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, WaiterError


def _now() -> float:
    """Поточний час (Unix epoch, с) — єдине джерело часу для журналу станів."""
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class EC2LifecycleManager:
    """Клас програмного керування життєвим циклом інстансів IaaS у хмарі AWS."""

    # Відповідність цільового стану -> вбудований очікувач (Waiter) Boto3
    WAITERS = {
        "running": "instance_running",
        "stopped": "instance_stopped",
        "terminated": "instance_terminated",
        "status_ok": "instance_status_ok",
    }

    def __init__(self, config_path: str, localstack: bool = False):
        """Ініціалізація менеджера та зчитування конфігураційного маніфесту.

        localstack=True перенаправляє всі виклики API на локальний емулятор
        LocalStack (http://localhost:4566) з фіктивними ключами "test"/"test".
        Сам код керування життєвим циклом при цьому не змінюється — у цьому
        й полягає перевага SDK: змінюється лише endpoint.
        """
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Конфігураційний файл не знайдено: {config_path}")

        with open(config_path, "r", encoding="utf-8") as file:
            self.config: Dict[str, Any] = json.load(file)

        self.localstack = localstack or os.environ.get("CPS_LOCALSTACK") == "1"
        self.ls_config: Dict[str, Any] = self.config.get("localstack", {})
        self.region = self.config.get("region_name", "eu-central-1")

        if self.localstack:
            endpoint = os.environ.get("LOCALSTACK_ENDPOINT",
                                      self.ls_config.get("endpoint_url", "http://localhost:4566"))
            # LocalStack приймає будь-які ключі; ~/.aws користувача не використовується
            self.session = boto3.Session(region_name=self.region,
                                         aws_access_key_id="test",
                                         aws_secret_access_key="test")
            self.ec2_client = self.session.client("ec2", endpoint_url=endpoint)
            self.ec2_resource = self.session.resource("ec2", endpoint_url=endpoint)
            print(f"[INFO] Режим LocalStack: API EC2 -> {endpoint} (регіон {self.region})")
        else:
            # Рівень сесії: регіон і креденшали (з ~/.aws або змінних середовища)
            self.session = boto3.Session(region_name=self.region)
            # Низькорівневий клієнт — 1:1 відображення на EC2 Query API
            self.ec2_client = self.session.client("ec2")
            # Високорівневий ресурс — ООП-обгортка (використовується для інстансу)
            self.ec2_resource = self.session.resource("ec2")

        self.instance_id: Optional[str] = None
        self.ami_id: Optional[str] = None
        self.root_device_name: str = "/dev/xvda"
        self.security_group_id: Optional[str] = None
        self.key_pair_name = self.config.get("key_pair_name", "cps-keypair")
        self.security_group_name = self.config.get("security_group_name", "cps-secgroup")
        self.mqtt_port = int(self.config.get("mqtt_port", 1883))
        self.mqtt_user = self.config.get("mqtt_username", "cps-sensor")
        self.mqtt_password: Optional[str] = None

        # Журнал подій життєвого циклу: [{event, state, ts, wait_s}]
        self.timeline: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ журнал
    def _log_event(self, event: str, state: str, wait_s: Optional[float] = None,
                   polls: Optional[int] = None) -> None:
        entry = {"event": event, "state": state, "timestamp": _iso(_now()), "ts": _now()}
        if wait_s is not None:
            entry["wait_seconds"] = round(wait_s, 2)
        if polls is not None:
            entry["polls"] = polls
        self.timeline.append(entry)

    # ------------------------------------------------------------ пошук AMI
    def resolve_ami(self) -> str:
        """Визначення AMI: явний ID з конфігурації або пошук останнього Debian 12."""
        configured = self.config.get("ami_id", "auto")
        if self.localstack:
            # У LocalStack AMI — це Docker-образ debian:12, позначений тегом
            # localstack-ec2/<name>:<ami-id> (робить scripts/localstack.sh start)
            self.ami_id = self.ls_config.get("ami_id", "ami-000003")
            try:
                self.ec2_client.describe_images(ImageIds=[self.ami_id])
            except ClientError as err:
                raise RuntimeError(
                    f"AMI {self.ami_id} не зареєстровано в LocalStack. "
                    "Виконайте: bash scripts/localstack.sh start") from err
            print(f"[INFO] LocalStack AMI {self.ami_id} "
                  f"(Docker-образ {self.ls_config.get('docker_image', 'debian:12')}).")
        elif configured and configured != "auto":
            self.ami_id = configured
        else:
            lookup = self.config.get("ami_lookup", {})
            print("[INFO] Пошук актуального AMI Debian 12 (офіційний акаунт Debian)...")
            response = self.ec2_client.describe_images(
                Owners=[lookup.get("owner_id", "136693071363")],
                Filters=[
                    {"Name": "name", "Values": [lookup.get("name_pattern", "debian-12-amd64-*")]},
                    {"Name": "architecture", "Values": [lookup.get("architecture", "x86_64")]},
                    {"Name": "state", "Values": ["available"]},
                    {"Name": "virtualization-type", "Values": ["hvm"]},
                ],
            )
            images = sorted(response.get("Images", []),
                            key=lambda img: img["CreationDate"], reverse=True)
            if not images:
                raise RuntimeError("AMI Debian 12 не знайдено в регіоні " + self.region)
            self.ami_id = images[0]["ImageId"]
            print(f"[SUCCESS] Обрано AMI {self.ami_id} ({images[0].get('Name')}).")

        # Ім'я кореневого пристрою беремо з самого образу: у Debian це /dev/xvda.
        # Якщо вказати /dev/sda1 (як в еталоні для Ubuntu), EC2 додасть окремий
        # диск, а кореневий том залишиться 8 ГБ — вимогу "EBS 15 ГБ" не буде виконано.
        image = self.ec2_client.describe_images(ImageIds=[self.ami_id])["Images"][0]
        self.root_device_name = image.get("RootDeviceName", "/dev/xvda")
        print(f"[INFO] Кореневий пристрій образу: {self.root_device_name}")
        return self.ami_id

    # --------------------------------------------------------------- SSH-ключ
    def ensure_key_pair(self) -> str:
        """Перевірка наявності або створення нової пари SSH-ключів."""
        try:
            self.ec2_client.describe_key_pairs(KeyNames=[self.key_pair_name])
            print(f"[INFO] SSH-ключ '{self.key_pair_name}' знайдено в інфраструктурі.")
            if not os.path.exists(f"{self.key_pair_name}.pem"):
                print(f"[WARN] Локального файлу {self.key_pair_name}.pem немає — "
                      "SSH-доступ буде неможливим (для MQTT-тесту він не потрібен).")
            return self.key_pair_name
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
                raise
            print(f"[INFO] Генерація нового SSH-ключа '{self.key_pair_name}'...")
            key_response = self.ec2_client.create_key_pair(
                KeyName=self.key_pair_name,
                KeyType="ed25519",
                TagSpecifications=[{"ResourceType": "key-pair", "Tags": self._tags()}],
            )
            private_key_path = f"{self.key_pair_name}.pem"
            # Старий файл від попереднього запуску має права 0400 — open("w")
            # на ньому дасть PermissionError, тому спершу видаляємо його.
            if os.path.exists(private_key_path):
                os.chmod(private_key_path, 0o600)
                os.remove(private_key_path)
            with open(private_key_path, "w", encoding="utf-8") as key_file:
                key_file.write(key_response["KeyMaterial"])
            os.chmod(private_key_path, 0o400)
            print(f"[SUCCESS] Приватний ключ збережено у файл: {private_key_path}")
            return self.key_pair_name

    # ---------------------------------------------------------- Security Group
    @staticmethod
    def _detect_public_ip_cidr() -> str:
        """Визначення публічної IP-адреси студента для обмеження SSH (/32).

        macOS: Python з інсталятора python.org не має кореневих сертифікатів
        (потрібно запускати "Install Certificates.command"), тому urllib може
        впасти з CERTIFICATE_VERIFY_FAILED. Тоді пробуємо certifi, а потім
        системний curl, який використовує Keychain macOS.
        """
        url = "https://checkip.amazonaws.com"
        ip = None
        try:
            context = ssl.create_default_context()
            try:
                import certifi  # встановлюється разом з boto3 не завжди — тому опційно
                context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                pass
            with urllib.request.urlopen(url, timeout=5, context=context) as resp:
                ip = resp.read().decode().strip()
        except Exception:  # noqa: BLE001
            curl = shutil.which("curl")
            if curl:
                try:
                    ip = subprocess.run([curl, "-fsS", "--max-time", "5", url],
                                        capture_output=True, text=True, check=True).stdout.strip()
                except (subprocess.SubprocessError, OSError):
                    ip = None
        try:
            socket.inet_aton(ip or "")  # валідація IPv4
            return f"{ip}/32"
        except OSError:
            print("[WARN] Не вдалося визначити ваш IP; SSH буде відкрито для 0.0.0.0/0.")
            return "0.0.0.0/0"

    def _ensure_localstack_default_sg(self) -> str:
        """LocalStack (Docker VM manager) враховує лише групу 'default': її
        ingress-порти під час створення інстансу пробрасуються з контейнера
        на випадкові порти 127.0.0.1 хоста. Тільки так до «інстансу» можна
        достукатися на macOS, де мережа контейнерів з хоста не видна."""
        group = self.ec2_client.describe_security_groups(GroupNames=["default"])["SecurityGroups"][0]
        group_id = group["GroupId"]
        for port, desc in ((22, "SSH admin access"), (self.mqtt_port, "MQTT sensors (Mosquitto)")):
            try:
                self.ec2_client.authorize_security_group_ingress(
                    GroupId=group_id,
                    IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": desc}]}],
                )
            except ClientError as err:
                if err.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise
        self.security_group_id = None  # групу default не видаляємо при --purge
        print(f"[SUCCESS] LocalStack: у групі default ({group_id}) відкрито 22/tcp, {self.mqtt_port}/tcp.")
        return group_id

    def ensure_security_group(self) -> str:
        """Створення та конфігурування правил мережевого екрана (Security Group)."""
        if self.localstack:
            return self._ensure_localstack_default_sg()

        vpc_response = self.ec2_client.describe_vpcs(
            Filters=[{"Name": "isDefault", "Values": ["true"]}]
        )
        if not vpc_response["Vpcs"]:
            raise RuntimeError("У регіоні немає Default VPC — створіть її (aws ec2 create-default-vpc).")
        default_vpc_id = vpc_response["Vpcs"][0]["VpcId"]

        existing = self.ec2_client.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [self.security_group_name]},
                {"Name": "vpc-id", "Values": [default_vpc_id]},
            ]
        )["SecurityGroups"]
        if existing:
            self.security_group_id = existing[0]["GroupId"]
            print(f"[INFO] Security Group '{self.security_group_name}' знайдено "
                  f"(ID: {self.security_group_id}).")
            return self.security_group_id

        print(f"[INFO] Створення Security Group '{self.security_group_name}'...")
        group_response = self.ec2_client.create_security_group(
            GroupName=self.security_group_name,
            Description="CPS sensor aggregator: SSH admin + MQTT 1883 for sensors",
            VpcId=default_vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": self._tags()}],
        )
        group_id = group_response["GroupId"]

        ssh_cidr = self.config.get("ssh_ingress_cidr", "auto")
        if ssh_cidr == "auto":
            ssh_cidr = self._detect_public_ip_cidr()
        mqtt_cidr = self.config.get("mqtt_ingress_cidr", "0.0.0.0/0")

        # Вхідні правила (Ingress). Вихідний трафік SG дозволяє за замовчуванням,
        # а сама SG є stateful — відповіді на дозволені з'єднання проходять автоматично.
        self.ec2_client.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                    "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH admin access"}],
                },
                {
                    "IpProtocol": "tcp", "FromPort": self.mqtt_port, "ToPort": self.mqtt_port,
                    "IpRanges": [{"CidrIp": mqtt_cidr, "Description": "MQTT sensors (Mosquitto)"}],
                },
            ],
        )
        self.security_group_id = group_id
        print(f"[SUCCESS] Security Group {group_id} створено: "
              f"22/tcp <- {ssh_cidr}, {self.mqtt_port}/tcp <- {mqtt_cidr}.")
        return group_id

    # ------------------------------------------------------------- User Data
    def render_user_data(self, template_path: str) -> str:
        """Підстановка параметрів MQTT у шаблон cloud-init та збереження креденшалів."""
        with open(template_path, "r", encoding="utf-8") as script_file:
            template = script_file.read()

        # token_urlsafe дає лише [A-Za-z0-9_-] — безпечно для bash і JSON
        self.mqtt_password = secrets.token_urlsafe(18)
        rendered = (template
                    .replace("__MQTT_PORT__", str(self.mqtt_port))
                    .replace("__MQTT_USER__", self.mqtt_user)
                    .replace("__MQTT_PASSWORD__", self.mqtt_password))

        if len(rendered.encode("utf-8")) > 16 * 1024:
            raise ValueError("User Data перевищує ліміт EC2 у 16 КБ.")

        os.makedirs("output", exist_ok=True)
        cred_path = os.path.join("output", "mqtt_credentials.json")
        with open(cred_path, "w", encoding="utf-8") as f:
            json.dump({"username": self.mqtt_user, "password": self.mqtt_password,
                       "port": self.mqtt_port}, f, indent=2)
        os.chmod(cred_path, 0o600)
        print(f"[INFO] Облікові дані MQTT збережено у {cred_path} (не додавайте у Git).")
        return rendered

    # ----------------------------------------------------------- створення ВМ
    def _tags(self, with_name: bool = True) -> List[Dict[str, str]]:
        tags = [{"Key": k, "Value": v} for k, v in self.config.get("tags", {}).items()]
        if with_name:
            tags.insert(0, {"Key": "Name", "Value": self.config.get("instance_name", "cps-node")})
        return tags

    def provision_instance(self, user_data_script_path: str) -> Dict[str, Any]:
        """Створення та запуск віртуальної машини згідно з конфігурацією."""
        self.resolve_ami()
        key_name = self.ensure_key_pair()
        group_id = self.ensure_security_group()
        user_data_content = self.render_user_data(user_data_script_path)

        block_device_mappings = [{
            "DeviceName": self.root_device_name,
            "Ebs": {
                "VolumeSize": int(self.config.get("volume_size_gb", 15)),
                "VolumeType": self.config.get("volume_type", "gp3"),
                "DeleteOnTermination": True,  # том знищується разом з інстансом
                "Encrypted": True,
            },
        }]

        print(f"[INFO] Запуск процедури створення інстансу типу '{self.config['instance_type']}' "
              f"(EBS {self.config.get('volume_size_gb')} ГБ)...")
        response = self.ec2_client.run_instances(
            ImageId=self.ami_id,
            InstanceType=self.config["instance_type"],
            KeyName=key_name,
            SecurityGroupIds=[group_id],
            MinCount=1,
            MaxCount=1,
            UserData=user_data_content,  # Boto3 сам кодує у Base64
            BlockDeviceMappings=block_device_mappings,
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},  # IMDSv2
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": self._tags()},
                {"ResourceType": "volume", "Tags": self._tags()},
            ],
        )

        instance = response["Instances"][0]
        self.instance_id = instance["InstanceId"]
        self._log_event("run_instances", instance["State"]["Name"])
        print(f"[SUCCESS] Запит прийнято. Створено інстанс {self.instance_id} "
              f"(стан: {instance['State']['Name']}).")
        return instance

    # -------------------------------------------------------------- очікувачі
    def wait_for_state(self, target_state: str = "running", max_attempts: int = 40,
                       delay: int = 15) -> float:
        """Очікування цільового стану через механізм Waiters. Повертає T_wait, с."""
        if not self.instance_id:
            raise ValueError("Ідентифікатор інстансу не визначено.")
        if target_state not in self.WAITERS:
            raise ValueError(f"Невідомий стан для очікувача: {target_state}")

        print(f"[INFO] Очікування переходу інстансу {self.instance_id} у стан '{target_state}' "
              f"(Delay={delay}s, MaxAttempts={max_attempts})...")
        waiter = self.ec2_client.get_waiter(self.WAITERS[target_state])
        started = _now()
        try:
            waiter.wait(InstanceIds=[self.instance_id],
                        WaiterConfig={"Delay": delay, "MaxAttempts": max_attempts})
        except WaiterError as err:
            raise RuntimeError(f"Очікувач '{target_state}' вичерпав ліміт: {err}") from err
        elapsed = _now() - started
        # Оцінка кількості ітерацій опитування k (перше опитування — одразу)
        polls = int(elapsed // delay) + 1
        self._log_event(f"wait_{target_state}", target_state, wait_s=elapsed, polls=polls)
        print(f"[SUCCESS] Інстанс {self.instance_id} у стані '{target_state}' "
              f"(T_wait = {elapsed:.1f} с, k ≈ {polls}).")
        return elapsed

    def _endpoint_str(self, data: Dict[str, Any]) -> str:
        if self.localstack:
            port = self._docker_host_port(self.mqtt_port)
            return f"mqtt://127.0.0.1:{port}" if port else "N/A (порт ще не проброшено)"
        return f"mqtt://{data.get('PublicIpAddress', 'N/A')}:{self.mqtt_port}"

    def _ssh_str(self, data: Dict[str, Any]) -> str:
        if self.localstack:
            port = self._docker_host_port(22)
            return (f"ssh -p {port} -i {self.key_pair_name}.pem root@127.0.0.1" if port
                    else "N/A")
        return (f"ssh -i {self.key_pair_name}.pem "
                f"{self.config.get('ssh_user', 'admin')}@{data.get('PublicIpAddress', 'N/A')}")

    # ------------------------------------------------------------- метадані
    def get_instance_details(self) -> Dict[str, Any]:
        """Отримання мережевих, системних атрибутів інстансу та параметрів тому EBS."""
        if not self.instance_id:
            raise ValueError("Ідентифікатор інстансу не визначено.")

        response = self.ec2_client.describe_instances(InstanceIds=[self.instance_id])
        data = response["Reservations"][0]["Instances"][0]

        type_info = self.ec2_client.describe_instance_types(
            InstanceTypes=[data["InstanceType"]]
        )["InstanceTypes"][0]

        root_volume = {}
        for mapping in data.get("BlockDeviceMappings", []):
            if mapping.get("DeviceName") == data.get("RootDeviceName"):
                vol_id = mapping["Ebs"]["VolumeId"]
                vol = self.ec2_client.describe_volumes(VolumeIds=[vol_id])["Volumes"][0]
                root_volume = {
                    "VolumeId": vol_id,
                    "Device": mapping["DeviceName"],
                    "SizeGiB": vol.get("Size"),
                    "VolumeType": vol.get("VolumeType"),
                    "Iops": vol.get("Iops"),
                    "Throughput": vol.get("Throughput"),
                    "Encrypted": vol.get("Encrypted"),
                }

        return {
            "InstanceId": data.get("InstanceId"),
            "InstanceType": data.get("InstanceType"),
            "vCPU": type_info.get("VCpuInfo", {}).get("DefaultVCpus"),
            "MemoryMiB": type_info.get("MemoryInfo", {}).get("SizeInMiB"),
            "NetworkPerformance": type_info.get("NetworkInfo", {}).get("NetworkPerformance"),
            "State": data.get("State", {}).get("Name"),
            "ImageId": data.get("ImageId"),
            "PublicIpAddress": data.get("PublicIpAddress", "N/A"),
            "PublicDnsName": data.get("PublicDnsName") or "N/A",
            "PrivateIpAddress": data.get("PrivateIpAddress", "N/A"),
            "VpcId": data.get("VpcId"),
            "SubnetId": data.get("SubnetId"),
            "SecurityGroups": [g["GroupId"] for g in data.get("SecurityGroups", [])],
            "KeyName": data.get("KeyName"),
            "AvailabilityZone": data.get("Placement", {}).get("AvailabilityZone"),
            "LaunchTime": str(data.get("LaunchTime")),
            "Architecture": data.get("Architecture"),
            "VirtualizationType": data.get("VirtualizationType"),
            "Hypervisor": data.get("Hypervisor"),
            "RootDeviceName": data.get("RootDeviceName"),
            "RootVolume": root_volume,
            "MqttEndpoint": self._endpoint_str(data),
            "SshCommand": self._ssh_str(data),
            "Backend": "LocalStack (Docker VM manager)" if self.localstack else "AWS",
            "Tags": {tag["Key"]: tag["Value"] for tag in data.get("Tags", [])},
        }

    # ------------------------------------------------- функціональний тест MQTT
    @staticmethod
    def _mqtt_str(value: str) -> bytes:
        raw = value.encode("utf-8")
        return struct.pack("!H", len(raw)) + raw

    @staticmethod
    def _mqtt_remaining_length(length: int) -> bytes:
        out = bytearray()
        while True:
            byte = length % 128
            length //= 128
            if length:
                byte |= 0x80
            out.append(byte)
            if not length:
                return bytes(out)

    # ---------------------------------------------- адреса брокера (AWS / LocalStack)
    def _docker_host_port(self, container_port: int) -> Optional[int]:
        """LocalStack: знаходить контейнер-«інстанс» і порт хоста, на який Docker
        пробросив container_port (правило з групи default)."""
        docker = shutil.which("docker")
        if not docker or not self.instance_id:
            return None

        def run(*args: str) -> str:
            try:
                return subprocess.run([docker, *args], capture_output=True, text=True,
                                      timeout=10).stdout.strip()
            except (subprocess.SubprocessError, OSError):
                return ""

        image = f"localstack-ec2/{self.ls_config.get('ami_name', 'debian-12-cps')}:" \
                f"{self.ls_config.get('ami_id', 'ami-000003')}"
        ids = run("ps", "-q", "--filter", f"name={self.instance_id}").split() \
            or run("ps", "-q", "--filter", f"ancestor={image}").split()
        if not ids:
            return None
        mapping = run("port", ids[0], f"{container_port}/tcp")  # напр. "0.0.0.0:51747"
        for line in mapping.splitlines():
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                return int(port)
        return None

    def mqtt_target(self, details: Dict[str, Any]) -> Optional[tuple]:
        """(host, port) для перевірки брокера: публічна IP в AWS або 127.0.0.1:<порт> у LocalStack."""
        if self.localstack:
            port = self._docker_host_port(self.mqtt_port)
            return ("127.0.0.1", port) if port else None
        ip = details.get("PublicIpAddress")
        return (ip, self.mqtt_port) if ip and ip != "N/A" else None

    def mqtt_probe(self, host: str, port: Optional[int] = None, timeout: float = 5.0) -> int:
        """Надсилає пакет MQTT 3.1.1 CONNECT і повертає код CONNACK (0 = прийнято)."""
        client_id = f"lab1-probe-{secrets.token_hex(3)}"
        flags = 0x02 | 0x80 | 0x40  # CleanSession + Username + Password
        variable_header = self._mqtt_str("MQTT") + bytes([0x04, flags]) + struct.pack("!H", 30)
        payload = (self._mqtt_str(client_id) + self._mqtt_str(self.mqtt_user)
                   + self._mqtt_str(self.mqtt_password or ""))
        body = variable_header + payload
        packet = bytes([0x10]) + self._mqtt_remaining_length(len(body)) + body

        with socket.create_connection((host, port or self.mqtt_port), timeout=timeout) as sock:
            sock.sendall(packet)
            connack = sock.recv(4)
            if len(connack) < 4 or connack[0] != 0x20:
                raise ConnectionError(f"Неочікувана відповідь брокера: {connack!r}")
            sock.sendall(bytes([0xE0, 0x00]))  # DISCONNECT
            return connack[3]

    def wait_for_mqtt(self, host: str, port: Optional[int] = None,
                      attempts: int = 30, delay: int = 10) -> float:
        """Очікування готовності брокера після cloud-init (власний polling-цикл)."""
        port = port or self.mqtt_port
        print(f"[INFO] Перевірка MQTT-брокера mqtt://{host}:{port} ...")
        started = _now()
        last_error: Any = None
        for attempt in range(1, attempts + 1):
            try:
                code = self.mqtt_probe(host, port)
                if code == 0:
                    elapsed = _now() - started
                    self._log_event("mqtt_connack_ok", "running", wait_s=elapsed, polls=attempt)
                    print(f"[SUCCESS] Mosquitto прийняв автентифіковане з'єднання "
                          f"(CONNACK=0, спроба {attempt}, {elapsed:.1f} с).")
                    return elapsed
                last_error = f"CONNACK return code {code}"
            except (OSError, ConnectionError) as err:
                last_error = err
            print(f"       спроба {attempt}/{attempts}: брокер ще не готовий ({last_error})")
            time.sleep(delay)
        raise RuntimeError(f"MQTT-брокер не відповів за {attempts * delay} с: {last_error}")

    # ------------------------------------------------------ керування станами
    def stop_instance(self) -> float:
        """Зупинка працюючого інстансу (RAM звільняється, том EBS зберігається)."""
        print(f"[INFO] Відправка сигналу зупинки інстансу {self.instance_id}...")
        resp = self.ec2_client.stop_instances(InstanceIds=[self.instance_id])
        self._log_event("stop_instances", resp["StoppingInstances"][0]["CurrentState"]["Name"])
        return self.wait_for_state("stopped")

    def start_instance(self) -> float:
        """Повторний запуск зупиненого інстансу."""
        print(f"[INFO] Відправка сигналу старту інстансу {self.instance_id}...")
        resp = self.ec2_client.start_instances(InstanceIds=[self.instance_id])
        self._log_event("start_instances", resp["StartingInstances"][0]["CurrentState"]["Name"])
        return self.wait_for_state("running")

    def terminate_instance(self) -> float:
        """Безповоротне знищення віртуальної машини разом із кореневим томом."""
        print(f"[INFO] Ініціалізація процедури утилізації (Termination) {self.instance_id}...")
        resp = self.ec2_client.terminate_instances(InstanceIds=[self.instance_id])
        self._log_event("terminate_instances",
                        resp["TerminatingInstances"][0]["CurrentState"]["Name"])
        elapsed = self.wait_for_state("terminated")
        print(f"[SUCCESS] Ресурси інстансу {self.instance_id} повністю вивільнено.")
        return elapsed

    def cleanup_network_and_keys(self) -> None:
        """Видалення Security Group і пари ключів (після terminated)."""
        if self.security_group_id:
            try:
                self.ec2_client.delete_security_group(GroupId=self.security_group_id)
                print(f"[SUCCESS] Security Group {self.security_group_id} видалено.")
            except ClientError as err:
                print(f"[WARN] SG не видалено: {err.response['Error']['Code']}")
        try:
            self.ec2_client.delete_key_pair(KeyName=self.key_pair_name)
            print(f"[SUCCESS] Пару ключів '{self.key_pair_name}' видалено з AWS.")
        except ClientError as err:
            print(f"[WARN] Ключ не видалено: {err.response['Error']['Code']}")

    # ------------------------------------------------ аналітика часу та вартості
    def lifecycle_report(self) -> Dict[str, Any]:
        """Розрахунок t_run, t_life, сумарного T_wait та C_total за формулою методички."""
        pricing = self.config.get("pricing", {})
        p_compute = float(pricing.get("compute_usd_per_hour", 0.012))
        p_storage = float(pricing.get("storage_usd_per_gb_month", 0.0952))
        min_bill = int(pricing.get("billing_min_seconds", 60))
        t_month = int(pricing.get("month_seconds", 2_592_000))
        volume_gb = int(self.config.get("volume_size_gb", 15))

        # t_run: сумарний час між "running" і наступним stop/terminate
        t_run, running_since = 0.0, None
        for ev in self.timeline:
            if ev["event"] == "wait_running":
                running_since = ev["ts"]
            elif ev["event"] in ("stop_instances", "terminate_instances") and running_since:
                t_run += ev["ts"] - running_since
                running_since = None

        # t_life: від запиту run_instances до підтвердженого terminated
        created = next((e["ts"] for e in self.timeline if e["event"] == "run_instances"), None)
        ended = next((e["ts"] for e in self.timeline if e["event"] == "wait_terminated"), _now())
        t_life = (ended - created) if created else 0.0

        compute_cost = max(min_bill, t_run) * p_compute / 3600
        storage_cost = volume_gb * p_storage * t_life / t_month
        # Кожен етап очікування окремо; повторні (running після start) нумеруються
        waits: Dict[str, float] = {}
        for e in self.timeline:
            if "wait_seconds" in e:
                label, n = e["event"], 2
                while label in waits:
                    label, n = f"{e['event']} #{n}", n + 1
                waits[label] = e["wait_seconds"]

        return {
            "t_run_seconds": round(t_run, 1),
            "t_life_seconds": round(t_life, 1),
            "total_wait_seconds": round(sum(v for v in waits.values() if v is not None), 1),
            "waits": waits,
            "P_compute_usd_per_hour": p_compute,
            "P_storage_usd_per_gb_month": p_storage,
            "V_EBS_gb": volume_gb,
            "compute_cost_usd": round(compute_cost, 6),
            "storage_cost_usd": round(storage_cost, 6),
            "C_total_usd": round(compute_cost + storage_cost, 6),
            "monthly_24x7_estimate_usd": round(p_compute * 730 + volume_gb * p_storage, 2),
            "note": ("LocalStack: вартість умовна, розрахована за тарифами AWS для порівняння"
                     if self.localstack else "AWS on-demand"),
            "timeline": [{k: v for k, v in e.items() if k != "ts"} for e in self.timeline],
        }
