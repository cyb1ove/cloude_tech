"""
Продюсер телеметрії кутових енкодерів маніпуляторів (варіант 3).

1000 повідомлень пакетами SendMessageBatch по 10 (100 викликів API замість 1000).
5 % (50) — «отруйні» повідомлення (poison pill): пошкоджене значення енкодера, яке
неможливо перетворити на число, — імітація збою АЦП або помилки серіалізації прошивки.
"""

import json
import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

JOINTS = ["J1", "J2", "J3", "J4", "J5", "J6"]


class TelemetryProducer:
    def __init__(self, sqs_client, queue_url: str, cfg: Dict[str, Any]):
        self.sqs = sqs_client
        self.url = queue_url
        self.cfg = cfg
        self.rng = random.Random(cfg.get("seed", 3))

    def build_messages(self) -> Tuple[List[Dict[str, Any]], Set[int]]:
        total = self.cfg["total_messages"]
        n_poison = round(total * self.cfg["poison_pill_percentage"] / 100)
        poison = set(self.rng.sample(range(1, total + 1), n_poison))
        msgs = []
        for seq in range(1, total + 1):
            robot = f"ROBOT-{(seq - 1) % self.cfg['robots'] + 1:02d}"
            is_poison = seq in poison
            angles = {j: round(self.rng.uniform(-math.pi, math.pi), 5) for j in JOINTS}
            if is_poison:
                angles[self.rng.choice(JOINTS)] = self.rng.choice(["CORRUPTED_STRING", "0xFFFF_ERR", "NaN?"])
            body = {
                "sequence_id": seq,
                "robot_id": robot,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "payload_type": "POISON_PILL" if is_poison else "TELEMETRY_SAMPLE",
                "encoder_angles_rad": angles,
                "angular_velocity_rad_s": {j: round(self.rng.uniform(-2.5, 2.5), 4) for j in JOINTS},
                "encoder_resolution_bits": 17,
            }
            msgs.append({
                "Id": str(seq),
                "MessageBody": json.dumps(body),
                "MessageAttributes": {
                    "RobotID": {"DataType": "String", "StringValue": robot},
                    "SchemaVersion": {"DataType": "Number", "StringValue": "2"},
                    "IsCorrupted": {"DataType": "String", "StringValue": str(is_poison)},
                },
            })
        return msgs, poison

    def send_all(self) -> Dict[str, Any]:
        msgs, poison = self.build_messages()
        print(f"[PRODUCER] Старт потоку з {len(msgs)} повідомлень (5 % poison: {len(poison)} msg), "
              f"SendMessageBatch по 10...", flush=True)
        failed, calls = [], 0
        t0 = time.perf_counter()
        for i in range(0, len(msgs), 10):
            batch = msgs[i:i + 10]
            for attempt in range(3):
                r = self.sqs.send_message_batch(QueueUrl=self.url, Entries=batch)
                calls += 1
                bad = {f["Id"] for f in r.get("Failed", [])}
                if not bad:
                    break
                batch = [m for m in batch if m["Id"] in bad]   # повторюємо лише невдалі записи
                time.sleep(0.2 * (attempt + 1))
            else:
                failed += [m["Id"] for m in batch]
        dt = time.perf_counter() - t0
        sent = len(msgs) - len(failed)
        print(f"[PRODUCER SUCCESS] Успішно відправлено {sent} повідомлень за {dt:.3f} с "
              f"({sent / dt:.1f} msg/s, {calls} викликів SendMessageBatch)", flush=True)
        return {"sent": sent, "failed": failed, "seconds": round(dt, 3), "throughput_msg_s": round(sent / dt, 1),
                "api_calls": calls, "poison_ids": sorted(poison)}
