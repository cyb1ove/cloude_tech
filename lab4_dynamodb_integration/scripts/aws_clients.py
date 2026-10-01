"""Фабрика клієнтів Boto3 для AWS і LocalStack (єдина точка налаштування endpoint)."""
import os
from typing import Any

import boto3
from botocore.config import Config

LOCALSTACK_DEFAULT = "http://localhost:4566"


def s3_config(localstack: bool) -> Config:
    kwargs: dict[str, Any] = {
        "signature_version": "s3v4",  # SigV4 обов'язковий для eu-central-1 і presigned URL
        # LocalStack працює за адресою localhost, тому path-style; в AWS — virtual-hosted
        "s3": {"addressing_style": "path" if localstack else "virtual"},
        "retries": {"max_attempts": 5, "mode": "standard"},
    }
    try:
        # botocore ≥ 1.36 додає контрольні суми CRC32 за замовчуванням; для presigned URL,
        # які використовує «простий» пристрій, обчислюємо їх лише коли це вимагає API.
        return Config(**kwargs, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required")
    except TypeError:
        return Config(**kwargs)


def session(region: str, localstack: bool) -> boto3.Session:
    if localstack:
        return boto3.Session(region_name=region, aws_access_key_id="test", aws_secret_access_key="test")
    return boto3.Session(region_name=region)


def endpoint(localstack: bool) -> str | None:
    return os.environ.get("LOCALSTACK_ENDPOINT", LOCALSTACK_DEFAULT) if localstack else None


def client(service: str, region: str, localstack: bool, **kwargs: Any):
    cfg = s3_config(localstack) if service == "s3" else None
    return session(region, localstack).client(service, endpoint_url=endpoint(localstack), config=cfg, **kwargs)
