"""
Керування таблицею DynamoDB для телеметрії роботизованих маніпуляторів (ЛБ4, варіант 3).

Схема:
    PK  RobotID   (S)  — ідентифікатор маніпулятора (рівномірний розподіл по розділах)
    SK  Timestamp (N)  — Unix epoch, с (діапазонні запити за часом)
    GSI GSI_OperationalState_JointTorque: PK OperationalState (S), SK JointTorqueNm (N), проєкція ALL
Режим оплати PAY_PER_REQUEST, TTL за атрибутом ExpiresAt, оптимістичне блокування за атрибутом Version.
"""

import json
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from scripts import aws_clients


def log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


class DynamoDBManager:
    def __init__(self, config_path: str, localstack: bool = False, table_name: Optional[str] = None):
        with open(config_path, encoding="utf-8") as f:
            self.cfg: Dict[str, Any] = json.load(f)
        self.region = self.cfg.get("region", "eu-central-1")
        self.localstack = localstack
        sess = aws_clients.session(self.region, localstack)
        ep = aws_clients.endpoint(localstack)
        self.client = sess.client("dynamodb", endpoint_url=ep)          # низькорівневий API
        self.resource = sess.resource("dynamodb", endpoint_url=ep)      # Table, batch_writer, Decimal
        self.table_name = table_name or f"{self.cfg['table_name_prefix']}-{uuid.uuid4().hex[:8]}"
        self.table = self.resource.Table(self.table_name)
        self.pk = self.cfg["primary_key"]["partition_key"]["name"]
        self.sk = self.cfg["primary_key"]["sort_key"]["name"]
        self.gsi = self.cfg["global_secondary_index"]

    # ------------------------------------------------------------ створення
    def create_table_with_gsi(self) -> float:
        pk, sk, g = self.cfg["primary_key"]["partition_key"], self.cfg["primary_key"]["sort_key"], self.gsi
        attrs = {a["name"]: a["type"] for a in (pk, sk, g["partition_key"], g["sort_key"])}
        log("INFO", f"Створення таблиці DynamoDB '{self.table_name}' у режимі {self.cfg['billing_mode']}...")
        t0 = time.time()
        self.client.create_table(
            TableName=self.table_name,
            BillingMode=self.cfg["billing_mode"],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": t} for n, t in attrs.items()],
            KeySchema=[{"AttributeName": pk["name"], "KeyType": "HASH"},
                       {"AttributeName": sk["name"], "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{
                "IndexName": g["index_name"],
                "KeySchema": [{"AttributeName": g["partition_key"]["name"], "KeyType": "HASH"},
                              {"AttributeName": g["sort_key"]["name"], "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": g["projection_type"]},
            }],
            SSESpecification={"Enabled": True},          # шифрування KMS (ключ aws/dynamodb)
            Tags=[{"Key": k, "Value": v} for k, v in self.cfg.get("tags", {}).items()],
        )
        log("INFO", "Очікування переходу таблиці у стан 'ACTIVE'...")
        self.client.get_waiter("table_exists").wait(TableName=self.table_name,
                                                    WaiterConfig={"Delay": 2, "MaxAttempts": 60})
        # GSI створюється асинхронно — чекаємо, доки він стане ACTIVE
        for _ in range(60):
            desc = self.client.describe_table(TableName=self.table_name)["Table"]
            if all(i.get("IndexStatus", "ACTIVE") == "ACTIVE" for i in desc.get("GlobalSecondaryIndexes", [])):
                break
            time.sleep(2)
        elapsed = time.time() - t0
        if self.cfg.get("ttl_attribute"):
            try:
                self.client.update_time_to_live(TableName=self.table_name, TimeToLiveSpecification={
                    "Enabled": True, "AttributeName": self.cfg["ttl_attribute"]})
                log("INFO", f"TTL увімкнено за атрибутом {self.cfg['ttl_attribute']} ({self.cfg['ttl_days']} діб).")
            except ClientError as e:
                log("WARN", f"TTL не налаштовано: {e.response['Error']['Code']}")
        log("SUCCESS", f"Таблиця '{self.table_name}' успішно створена та активна ({elapsed:.1f} с).")
        return elapsed

    def describe(self) -> Dict[str, Any]:
        t = self.client.describe_table(TableName=self.table_name)["Table"]
        return {
            "TableName": t["TableName"], "Status": t["TableStatus"],
            "KeySchema": t["KeySchema"], "AttributeDefinitions": t["AttributeDefinitions"],
            "BillingMode": t.get("BillingModeSummary", {}).get("BillingMode", self.cfg["billing_mode"]),
            "GSI": [{"IndexName": i["IndexName"], "KeySchema": i["KeySchema"],
                     "Projection": i["Projection"]["ProjectionType"], "Status": i.get("IndexStatus")}
                    for i in t.get("GlobalSecondaryIndexes", [])],
            "ItemCount": t.get("ItemCount"), "TableSizeBytes": t.get("TableSizeBytes"),
            "SSE": t.get("SSEDescription", {}).get("Status"),
        }

    # ------------------------------------------------------------- запис
    def batch_write_telemetry(self, items: List[Dict[str, Any]]) -> float:
        """BatchWriteItem по 25 елементів; batch_writer сам повторює UnprocessedItems."""
        log("INFO", f"Пакетне завантаження {len(items)} елементів телеметрії...")
        t0 = time.time()
        with self.table.batch_writer(overwrite_by_pkeys=[self.pk, self.sk]) as bw:
            for it in items:
                bw.put_item(Item=it)
        dt = time.time() - t0
        log("SUCCESS", f"Завантажено {len(items)} елементів за {dt:.3f} с "
                       f"({len(items) / dt:.0f} елементів/с, {len(items) // 25 + (len(items) % 25 > 0)} запитів BatchWriteItem).")
        return dt

    def put_if_absent(self, item: Dict[str, Any]) -> bool:
        """Ідемпотентна вставка: attribute_not_exists запобігає перезапису наявного запису."""
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(#pk)",
                                ExpressionAttributeNames={"#pk": self.pk})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def conditional_update(self, robot_id: str, ts: int, expected_version: int,
                           new_values: Dict[str, Any]) -> Tuple[bool, Optional[int]]:
        """Оптимістичне блокування: оновлення лише якщо Version не змінилась з моменту читання."""
        sets = ", ".join(f"#{k} = :{k}" for k in new_values)
        names = {f"#{k}": k for k in new_values}
        names["#v"] = "Version"
        values = {f":{k}": v for k, v in new_values.items()}
        values.update({":expected": expected_version, ":one": 1})
        try:
            r = self.table.update_item(
                Key={self.pk: robot_id, self.sk: ts},
                UpdateExpression=f"SET {sets}, #v = #v + :one",
                ConditionExpression="#v = :expected",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values,
                ReturnValues="UPDATED_NEW",
            )
            return True, int(r["Attributes"]["Version"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False, None
            raise

    def get_item(self, robot_id: str, ts: int, consistent: bool = True) -> Optional[Dict[str, Any]]:
        return self.table.get_item(Key={self.pk: robot_id, self.sk: ts}, ConsistentRead=consistent).get("Item")

    # ---------------------------------------------------------------- читання
    @staticmethod
    def _capacity(resp: Dict[str, Any]) -> float:
        cc = resp.get("ConsumedCapacity") or {}
        return float(cc.get("CapacityUnits", 0.0))

    def query_telemetry_by_device_range(self, robot_id: str, start_ts: int, end_ts: int,
                                        consistent: bool = False) -> Dict[str, Any]:
        """Query: KeyConditionExpression — лише розділ робота й діапазон часу (O(log N + K))."""
        items, scanned, rcu, pages = [], 0, 0.0, 0
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key(self.pk).eq(robot_id) & Key(self.sk).between(start_ts, end_ts),
            "ConsistentRead": consistent, "ReturnConsumedCapacity": "TOTAL"}
        t0 = time.perf_counter()
        while True:
            r = self.table.query(**kwargs)
            items += r.get("Items", []); scanned += r.get("ScannedCount", 0); rcu += self._capacity(r); pages += 1
            if "LastEvaluatedKey" not in r:
                break
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return {"items": items, "count": len(items), "scanned": scanned, "rcu": rcu, "pages": pages,
                "latency_ms": (time.perf_counter() - t0) * 1000}

    def query_gsi_by_state_torque(self, state: str, min_torque: float) -> Dict[str, Any]:
        """Query по GSI: OperationalState = :s AND JointTorqueNm >= :t (завжди eventually consistent)."""
        items, scanned, rcu, pages = [], 0, 0.0, 0
        kwargs: Dict[str, Any] = {
            "IndexName": self.gsi["index_name"],
            "KeyConditionExpression": Key(self.gsi["partition_key"]["name"]).eq(state) &
                                      Key(self.gsi["sort_key"]["name"]).gte(Decimal(str(min_torque))),
            "ReturnConsumedCapacity": "TOTAL"}
        t0 = time.perf_counter()
        while True:
            r = self.table.query(**kwargs)
            items += r.get("Items", []); scanned += r.get("ScannedCount", 0); rcu += self._capacity(r); pages += 1
            if "LastEvaluatedKey" not in r:
                break
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return {"items": items, "count": len(items), "scanned": scanned, "rcu": rcu, "pages": pages,
                "latency_ms": (time.perf_counter() - t0) * 1000}

    def scan_telemetry_by_status(self, target_status: str) -> Dict[str, Any]:
        """Scan: читає ВСЮ таблицю (сторінки по 1 МБ), FilterExpression застосовується після читання."""
        items, scanned, rcu, pages = [], 0, 0.0, 0
        kwargs: Dict[str, Any] = {"FilterExpression": Attr("OperationalState").eq(target_status),
                                  "ReturnConsumedCapacity": "TOTAL"}
        t0 = time.perf_counter()
        while True:
            r = self.table.scan(**kwargs)
            items += r.get("Items", []); scanned += r.get("ScannedCount", 0); rcu += self._capacity(r); pages += 1
            if "LastEvaluatedKey" not in r:
                break
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return {"items": items, "count": len(items), "scanned": scanned, "rcu": rcu, "pages": pages,
                "latency_ms": (time.perf_counter() - t0) * 1000}

    # ------------------------------------------------------------ видалення
    def delete_table(self) -> None:
        log("INFO", f"Видалення таблиці {self.table_name}...")
        self.client.delete_table(TableName=self.table_name)
        self.client.get_waiter("table_not_exists").wait(TableName=self.table_name,
                                                        WaiterConfig={"Delay": 2, "MaxAttempts": 60})
        log("SUCCESS", f"Таблицю {self.table_name} успішно видалено.")
