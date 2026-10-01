"""
Пул обробників телеметрії (ThreadPoolExecutor, 8 потоків для варіанта 3).

Кожен потік: long polling ReceiveMessage (до 10 повідомлень, WaitTimeSeconds=2) →
валідація й обробка → DeleteMessageBatch лише для успішно оброблених повідомлень.
«Отруйні» повідомлення НЕ видаляються: після VisibilityTimeout вони знову стають видимими,
а коли ApproximateReceiveCount перевищить maxReceiveCount (2), SQS переносить їх у DLQ.
Доставка «принаймні один раз» → обробник ідемпотентний (облік sequence_id).
"""

import json
import math
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List


class PoisonPillError(ValueError):
    pass


def process_sample(body: Dict[str, Any]) -> float:
    """Валідація і «обробка»: перевірка діапазону кутів та оцінка вильоту схвату (спрощена кінематика)."""
    angles = body["encoder_angles_rad"]
    vals = []
    for j, v in angles.items():
        try:
            x = float(v)
        except (TypeError, ValueError) as e:
            raise PoisonPillError(f"{j}: пошкоджене значення енкодера {v!r}") from e
        if not math.isfinite(x) or abs(x) > 2 * math.pi:
            raise PoisonPillError(f"{j}: значення поза діапазоном {x}")
        vals.append(x)
    links = [0.40, 0.35, 0.25, 0.10, 0.08, 0.05]         # довжини ланок, м
    reach_x = sum(l * math.cos(sum(vals[:i + 1])) for i, l in enumerate(links))
    reach_y = sum(l * math.sin(sum(vals[:i + 1])) for i, l in enumerate(links))
    return math.hypot(reach_x, reach_y)


class TelemetryConsumerPool:
    def __init__(self, client_factory: Callable[[], Any], queue_url: str, cfg: Dict[str, Any]):
        self.client_factory = client_factory
        self.url = queue_url
        self.cfg = cfg
        self.lock = threading.Lock()
        self.seen_sequences: set = set()
        self.proc_times_ms: List[float] = []

    def run_wave(self, wave: int) -> Dict[str, Any]:
        k = self.cfg["consumer_threads"]
        stats = {"valid": 0, "poison_errors": 0, "duplicates": 0, "receives": 0, "empty_receives": 0,
                 "delete_calls": 0, "receive_counts": {}, "per_worker": {}}
        print(f"[CONSUMER] Хвиля {wave}: запуск пулу з {k} потоків-обробників...", flush=True)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=k, thread_name_prefix="sqs-worker") as ex:
            for f in [ex.submit(self._worker, w, stats) for w in range(1, k + 1)]:
                f.result()
        stats["seconds"] = round(time.perf_counter() - t0, 3)
        print(f"[CONSUMER FINISHED] Хвиля {wave}: оброблено {stats['valid']} коректних, "
              f"{stats['poison_errors']} помилок poison pill, дублікатів {stats['duplicates']} "
              f"за {stats['seconds']} с", flush=True)
        return stats

    def _worker(self, worker_id: int, stats: Dict[str, Any]) -> None:
        sqs = self.client_factory()
        rng = random.Random(worker_id)
        pt = self.cfg["processing_time_ms"]
        empty, ok, bad, busy = 0, 0, 0, 0.0
        while empty < self.cfg["empty_polls_to_stop"]:
            r = sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=10,
                                    WaitTimeSeconds=self.cfg["receive_wait_time_seconds"],
                                    AttributeNames=["ApproximateReceiveCount"], MessageAttributeNames=["All"])
            msgs = r.get("Messages", [])
            with self.lock:
                stats["receives"] += 1
            if not msgs:
                empty += 1
                with self.lock:
                    stats["empty_receives"] += 1
                continue
            empty = 0
            to_delete = []
            for m in msgs:
                rc = int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1))
                t_start = time.perf_counter()
                try:
                    try:
                        body = json.loads(m["Body"])
                    except json.JSONDecodeError as e:
                        raise PoisonPillError("некоректний JSON") from e
                    process_sample(body)
                    time.sleep(max(0.0, rng.gauss(pt["mean"], pt["stddev"])) / 1000)  # імітація обробки/запису в БД
                    seq = body["sequence_id"]
                    with self.lock:
                        dup = seq in self.seen_sequences
                        self.seen_sequences.add(seq)
                        stats["duplicates" if dup else "valid"] += 1
                    to_delete.append({"Id": m["MessageId"], "ReceiptHandle": m["ReceiptHandle"]})
                    ok += 1
                except PoisonPillError:
                    # НЕ видаляємо: повідомлення повернеться після VisibilityTimeout → після 2 спроб у DLQ
                    with self.lock:
                        stats["poison_errors"] += 1
                        stats["receive_counts"][str(rc)] = stats["receive_counts"].get(str(rc), 0) + 1
                    bad += 1
                dt = (time.perf_counter() - t_start) * 1000
                busy += dt / 1000
                with self.lock:
                    self.proc_times_ms.append(dt)
            if to_delete:
                sqs.delete_message_batch(QueueUrl=self.url, Entries=to_delete)   # 1 виклик замість до 10
                with self.lock:
                    stats["delete_calls"] += 1
        with self.lock:
            stats["per_worker"][f"W{worker_id}"] = {"ok": ok, "poison": bad,
                                                    "mu_msg_s": round(ok / busy, 1) if busy else None}

    def processing_stats(self) -> Dict[str, float]:
        t = self.proc_times_ms
        if len(t) < 2:
            return {}
        return {"mean_ms": round(statistics.fmean(t), 2), "stdev_ms": round(statistics.stdev(t), 2),
                "p99_ms": round(sorted(t)[int(0.99 * (len(t) - 1))], 2), "max_ms": round(max(t), 2), "n": len(t)}
