"""
Керування чергами SQS з Dead-Letter Queue (ЛБ5, варіант 3: cps-robot-telemetry).

    DLQ   cps-robot-telemetry-dlq-<hex>  — утримання 14 діб, RedriveAllowPolicy лише для основної черги
    Main  cps-robot-telemetry-<hex>      — VisibilityTimeout 10 с, утримання 4 доби,
                                           RedrivePolicy {deadLetterTargetArn, maxReceiveCount: 2}
Обидві черги зашифровані SSE-SQS (ключ, керований SQS).
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

from scripts import aws_clients


def log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


class SQSQueueManager:
    def __init__(self, cfg: Dict[str, Any], localstack: bool = False, suffix: Optional[str] = None):
        self.cfg = cfg
        self.region = cfg.get("region", "eu-central-1")
        self.localstack = localstack
        self.sqs = aws_clients.client("sqs", self.region, localstack)
        sfx = suffix or uuid.uuid4().hex[:8]
        self.main_queue_name = f"{cfg['queue_prefix']}-{sfx}"
        self.dlq_name = f"{cfg['queue_prefix']}-dlq-{sfx}"
        self.main_url: Optional[str] = None
        self.dlq_url: Optional[str] = None

    def new_client(self):
        """Окремий клієнт для кожного потоку-обробника (сесії boto3 не потокобезпечні)."""
        return aws_clients.client("sqs", self.region, self.localstack)

    def _arn(self, url: str) -> str:
        return self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    def setup_queues_with_dlq(self) -> Tuple[str, str]:
        tags = self.cfg.get("tags", {})
        log("INFO", f"Створення черги мертвих листів (DLQ): '{self.dlq_name}'...")
        self.dlq_url = self.sqs.create_queue(QueueName=self.dlq_name, tags=tags, Attributes={
            "MessageRetentionPeriod": str(self.cfg["dlq_retention_seconds"]),   # DLQ зберігає довше, ніж основна
            "SqsManagedSseEnabled": "true",
        })["QueueUrl"]
        dlq_arn = self._arn(self.dlq_url)
        log("SUCCESS", f"DLQ створено. URL: {self.dlq_url}")

        redrive = {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(self.cfg["max_receive_count"])}
        log("INFO", f"Створення основної черги '{self.main_queue_name}' (VisibilityTimeout="
                    f"{self.cfg['visibility_timeout_seconds']} с, maxReceiveCount={self.cfg['max_receive_count']})...")
        self.main_url = self.sqs.create_queue(QueueName=self.main_queue_name, tags=tags, Attributes={
            "VisibilityTimeout": str(self.cfg["visibility_timeout_seconds"]),
            "MessageRetentionPeriod": str(self.cfg["message_retention_seconds"]),
            "ReceiveMessageWaitTimeSeconds": str(self.cfg["receive_wait_time_seconds"]),
            "RedrivePolicy": json.dumps(redrive),
            "SqsManagedSseEnabled": "true",
        })["QueueUrl"]
        main_arn = self._arn(self.main_url)
        # Дозволяємо використовувати DLQ лише цій основній черзі
        try:
            self.sqs.set_queue_attributes(QueueUrl=self.dlq_url, Attributes={"RedriveAllowPolicy": json.dumps(
                {"redrivePermission": "byQueue", "sourceQueueArns": [main_arn]})})
        except ClientError as e:
            log("WARN", f"RedriveAllowPolicy не застосовано: {e.response['Error']['Code']}")
        log("SUCCESS", f"Основну чергу створено. URL: {self.main_url}")
        return self.main_url, self.dlq_url

    def get_queue_metrics(self, queue_url: str) -> Dict[str, int]:
        a = self.sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed"])["Attributes"]
        return {"Available": int(a.get("ApproximateNumberOfMessages", 0)),
                "InFlight": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
                "Delayed": int(a.get("ApproximateNumberOfMessagesDelayed", 0))}

    def wait_metrics_settle(self, queue_url: str, expected_total: Optional[int] = None, timeout: int = 30) -> Dict[str, int]:
        """Лічильники SQS приблизні й оновлюються з затримкою — чекаємо стабілізації."""
        last, stable = None, 0
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = self.get_queue_metrics(queue_url)
            total = m["Available"] + m["InFlight"]
            if m == last:
                stable += 1
            else:
                stable = 0
            if (expected_total is not None and total == expected_total) or stable >= 2:
                return m
            last = m
            time.sleep(2)
        return last or {}

    def describe(self, queue_url: str) -> Dict[str, Any]:
        return self.sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])["Attributes"]

    def peek_dlq(self, max_messages: int = 100) -> List[Dict[str, Any]]:
        """Читає повідомлення DLQ для аналізу й одразу повертає їх у чергу (VisibilityTimeout=0)."""
        out: List[Dict[str, Any]] = []
        seen = set()
        for _ in range(30):
            r = self.sqs.receive_message(QueueUrl=self.dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                         VisibilityTimeout=30, MessageAttributeNames=["All"],
                                         AttributeNames=["ApproximateReceiveCount", "SentTimestamp",
                                                         "ApproximateFirstReceiveTimestamp"])
            msgs = r.get("Messages", [])
            if not msgs:
                break
            for m in msgs:
                if m["MessageId"] in seen:
                    continue
                seen.add(m["MessageId"])
                out.append(m)
            if len(out) >= max_messages:
                break
        # повертаємо видимість, щоб повідомлення залишились у DLQ для перевірки AWS CLI
        for m in out:
            try:
                self.sqs.change_message_visibility(QueueUrl=self.dlq_url, ReceiptHandle=m["ReceiptHandle"],
                                                   VisibilityTimeout=0)
            except ClientError:
                pass
        return out

    def redrive_dlq(self) -> Optional[str]:
        """Повернення повідомлень з DLQ у вихідну чергу (StartMessageMoveTask) після виправлення помилки."""
        try:
            return self.sqs.start_message_move_task(SourceArn=self._arn(self.dlq_url))["TaskHandle"]
        except Exception as e:  # noqa: BLE001 — емулятори можуть не реалізовувати цю дію
            code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
            log("WARN", f"StartMessageMoveTask недоступний у цьому середовищі: {code}")
            return None

    def delete_queues(self) -> None:
        for url in (self.main_url, self.dlq_url):
            if url:
                try:
                    self.sqs.delete_queue(QueueUrl=url)
                    log("SUCCESS", f"Чергу видалено: {url.rsplit('/', 1)[-1]}")
                except ClientError as e:
                    log("WARN", f"{url}: {e.response['Error']['Code']}")
