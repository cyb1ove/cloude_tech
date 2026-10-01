"""
Керування захищеним об'єктним сховищем S3 для телеметрії КФС (ЛБ3, варіант 3).

Бакет cps-smart-meter-<8 hex> створюється з:
    * S3 Block Public Access (усі 4 параметри) та Object Ownership = BucketOwnerEnforced (ACL вимкнено);
    * версійністю об'єктів;
    * серверним шифруванням за замовчуванням (SSE-S3 AES256 або SSE-KMS + S3 Bucket Key);
    * політикою бакета, що забороняє доступ без TLS (aws:SecureTransport = false);
    * правилами життєвого циклу: Standard → Standard-IA (60 д) → Glacier (120 д) → видалення (730 д),
      неактуальні версії → Glacier (30 д) → видалення (90 д), очищення незавершених multipart (7 д);
    * тегами для обліку витрат.
Завантаження від пристроїв — лише через Presigned URL з обмеженим TTL (300 с).
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from scripts import aws_clients


def log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


class S3StorageManager:
    """Створення, налаштування, аудит і видалення бакета телеметрії."""

    def __init__(self, config_path: str, localstack: bool = False, bucket_name: Optional[str] = None):
        with open(config_path, encoding="utf-8") as f:
            self.cfg: Dict[str, Any] = json.load(f)
        self.region = self.cfg.get("region", "eu-central-1")
        self.localstack = localstack
        self.s3 = aws_clients.client("s3", self.region, localstack)
        # Унікальне ім'я: префікс варіанта + 8 шістнадцяткових символів (імена бакетів глобальні)
        self.bucket_name = bucket_name or f"{self.cfg['bucket_prefix']}-{uuid.uuid4().hex[:8]}"
        self.ttl = int(self.cfg.get("presigned_url_expiration_seconds", 300))

    # ------------------------------------------------------------------ створення
    def create_secure_bucket(self) -> str:
        b = self.bucket_name
        log("INFO", f"Створення об'єктного бакета '{b}' у регіоні {self.region}...")
        params: Dict[str, Any] = {"Bucket": b, "ObjectOwnership": "BucketOwnerEnforced"}
        if self.region != "us-east-1":  # для us-east-1 LocationConstraint не вказується
            params["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self.s3.create_bucket(**params)
        self.s3.get_waiter("bucket_exists").wait(Bucket=b, WaiterConfig={"Delay": 2, "MaxAttempts": 20})
        log("SUCCESS", f"Бакет '{b}' успішно створено.")

        if self.cfg.get("block_public_access", True):
            log("INFO", "Активація S3 Block Public Access (повна ізоляція)...")
            self.s3.put_public_access_block(Bucket=b, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True})

        if self.cfg.get("enable_versioning", True):
            log("INFO", "Увімкнення версійності об'єктів (Bucket Versioning)...")
            self.s3.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})

        self._apply_encryption()
        if self.cfg.get("enforce_tls_policy", True):
            if self.localstack:
                # LocalStack доступний лише по HTTP: політика aws:SecureTransport=false заблокувала б усі запити
                log("INFO", "LocalStack: політику DenyInsecureTransport не застосовано (емулятор працює по HTTP).")
            else:
                self._apply_bucket_policy()
        self._apply_lifecycle_policy()
        self.s3.put_bucket_tagging(Bucket=b, Tagging={"TagSet": [
            {"Key": k, "Value": v} for k, v in self.cfg.get("tags", {}).items()]})
        log("SUCCESS", "Бакет налаштовано: BPA, версійність, шифрування, політика TLS, життєвий цикл, теги.")
        return b

    def _apply_encryption(self) -> None:
        algo = self.cfg.get("encryption_algorithm", "AES256")
        rule: Dict[str, Any] = {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": algo}}
        if algo == "aws:kms":
            if self.cfg.get("kms_key_id"):
                rule["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"] = self.cfg["kms_key_id"]
            rule["BucketKeyEnabled"] = True  # S3 Bucket Key зменшує кількість викликів KMS (і вартість)
        log("INFO", f"Налаштування серверного шифрування за замовчуванням ({algo})...")
        self.s3.put_bucket_encryption(Bucket=self.bucket_name,
                                      ServerSideEncryptionConfiguration={"Rules": [rule]})

    def _apply_bucket_policy(self) -> None:
        """Заборона будь-яких запитів без TLS (у т.ч. presigned URL через http://)."""
        arn = f"arn:aws:s3:::{self.bucket_name}"
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [arn, f"{arn}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }],
        }
        log("INFO", "Застосування політики бакета DenyInsecureTransport (лише HTTPS)...")
        self.s3.put_bucket_policy(Bucket=self.bucket_name, Policy=json.dumps(policy))

    def _apply_lifecycle_policy(self) -> None:
        lc = self.cfg["lifecycle_rules"]
        log("INFO", "Конфігурування правил життєвого циклу (Lifecycle Policy)...")
        rule = {
            "ID": lc["rule_id"],
            "Status": "Enabled",
            "Filter": {"Prefix": lc["prefix"]},
            "Transitions": [
                {"Days": lc["transition_ia_days"], "StorageClass": "STANDARD_IA"},
                {"Days": lc["transition_glacier_days"], "StorageClass": "GLACIER"},
            ],
            "Expiration": {"Days": lc["expiration_days"]},
            "NoncurrentVersionTransitions": [
                {"NoncurrentDays": lc["noncurrent_glacier_days"], "StorageClass": "GLACIER"}],
            "NoncurrentVersionExpiration": {"NoncurrentDays": lc["noncurrent_expiration_days"]},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": lc["abort_multipart_days"]},
        }
        self.s3.put_bucket_lifecycle_configuration(Bucket=self.bucket_name,
                                                   LifecycleConfiguration={"Rules": [rule]})
        log("SUCCESS", f"Правила життєвого циклу застосовано: STANDARD → STANDARD_IA ({lc['transition_ia_days']} д) → "
                       f"GLACIER ({lc['transition_glacier_days']} д) → видалення ({lc['expiration_days']} д).")

    # ---------------------------------------------------------------- presigned
    def generate_presigned_url(self, object_key: str, client_method: str = "put_object",
                               expires_in: Optional[int] = None, content_type: Optional[str] = None) -> str:
        """Тимчасове делеговане право на одну операцію з одним об'єктом (SigV4, query-string)."""
        params: Dict[str, Any] = {"Bucket": self.bucket_name, "Key": object_key}
        if content_type and client_method == "put_object":
            params["ContentType"] = content_type  # підписаний заголовок: інший Content-Type → 403
        return self.s3.generate_presigned_url(ClientMethod=client_method, Params=params,
                                              ExpiresIn=expires_in or self.ttl)

    # ------------------------------------------------------------------- аудит
    def list_bucket_objects(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket_name):
            for o in page.get("Contents", []):
                out.append({"Key": o["Key"], "Size": o["Size"],
                            "StorageClass": o.get("StorageClass", "STANDARD"), "ETag": o["ETag"].strip('"')})
        return out

    def list_object_versions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for page in self.s3.get_paginator("list_object_versions").paginate(Bucket=self.bucket_name):
            for v in page.get("Versions", []):
                out.append({"Key": v["Key"], "VersionId": v["VersionId"], "IsLatest": v["IsLatest"],
                            "Size": v["Size"], "Type": "version", "LastModified": str(v["LastModified"])})
            for m in page.get("DeleteMarkers", []):
                out.append({"Key": m["Key"], "VersionId": m["VersionId"], "IsLatest": m["IsLatest"],
                            "Size": 0, "Type": "DELETE MARKER", "LastModified": str(m["LastModified"])})
        return sorted(out, key=lambda x: (x["Key"], x["LastModified"]), reverse=False)

    def head(self, key: str, version_id: Optional[str] = None) -> Dict[str, Any]:
        kw = {"Bucket": self.bucket_name, "Key": key}
        if version_id:
            kw["VersionId"] = version_id
        return self.s3.head_object(**kw)

    def security_posture(self) -> Dict[str, Any]:
        b = self.bucket_name
        posture: Dict[str, Any] = {}
        try:
            posture["public_access_block"] = self.s3.get_public_access_block(Bucket=b)["PublicAccessBlockConfiguration"]
        except ClientError as e:
            posture["public_access_block"] = f"ERROR {e.response['Error']['Code']}"
        posture["versioning"] = self.s3.get_bucket_versioning(Bucket=b).get("Status")
        enc = self.s3.get_bucket_encryption(Bucket=b)["ServerSideEncryptionConfiguration"]["Rules"][0]
        posture["encryption"] = enc["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        try:
            pol = json.loads(self.s3.get_bucket_policy(Bucket=b)["Policy"])
            posture["policy_statements"] = [s.get("Sid") for s in pol.get("Statement", [])]
        except ClientError:
            posture["policy_statements"] = []
        try:
            posture["ownership"] = self.s3.get_bucket_ownership_controls(Bucket=b)["OwnershipControls"]["Rules"][0]["ObjectOwnership"]
        except (ClientError, KeyError, IndexError):
            posture["ownership"] = "n/a"
        posture["lifecycle"] = self.s3.get_bucket_lifecycle_configuration(Bucket=b)["Rules"]
        return posture

    # ------------------------------------------------------------------ видалення
    def delete_bucket(self) -> None:
        """Видаляє всі версії та маркери видалення, потім сам бакет."""
        b = self.bucket_name
        log("INFO", f"Видалення всіх версій об'єктів і бакета '{b}'...")
        batch: List[Dict[str, str]] = []
        for page in self.s3.get_paginator("list_object_versions").paginate(Bucket=b):
            for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
                batch.append({"Key": v["Key"], "VersionId": v["VersionId"]})
                if len(batch) == 1000:
                    self.s3.delete_objects(Bucket=b, Delete={"Objects": batch, "Quiet": True})
                    batch = []
        if batch:
            self.s3.delete_objects(Bucket=b, Delete={"Objects": batch, "Quiet": True})
        self.s3.delete_bucket(Bucket=b)
        for _ in range(10):
            try:
                self.s3.head_bucket(Bucket=b)
                time.sleep(1)
            except ClientError:
                break
        log("SUCCESS", f"Бакет '{b}' видалено.")
