"""
Емулятор розумного лічильника електроенергії (варіант 3).

Пристрій НЕ має облікових даних AWS: він отримує від бекенду одноразовий
Presigned URL і виконує звичайний HTTP PUT. Тому тут немає boto3 — лише requests.
Кожен пакет ≈ 2 КБ JSON: миттєві трифазні вимірювання + профіль навантаження за 15 хв.
"""

import hashlib
import json
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import requests


class SensorTelemetryEmulator:
    def __init__(self, device_id: str, sensor_type: str, packet_size_bytes: int = 2048,
                 interval_minutes: int = 15, seed: int = 3):
        self.device_id = device_id
        self.sensor_type = sensor_type
        self.packet_size = packet_size_bytes
        self.interval = interval_minutes
        self.rng = random.Random(seed)
        self.energy_kwh = 15230.0 + self.rng.uniform(0, 50)  # накопичувальний лічильник
        self.seq = 0

    def _phase(self, nominal: float, spread: float) -> float:
        return round(nominal + self.rng.uniform(-spread, spread), 2)

    def generate_packet(self, ts: datetime) -> Dict[str, Any]:
        self.seq += 1
        hour = ts.hour + ts.minute / 60
        base_kw = 2.2 + 1.6 * max(0.0, math.sin((hour - 6) / 24 * 2 * math.pi))  # добовий профіль
        profile = [round(base_kw + self.rng.uniform(-0.35, 0.35), 3) for _ in range(self.interval)]
        power_kw = round(sum(profile) / len(profile), 3)
        self.energy_kwh = round(self.energy_kwh + power_kw * self.interval / 60, 3)
        v = [self._phase(230.0, 6.0) for _ in range(3)]
        pf = round(self.rng.uniform(0.86, 0.99), 3)
        status = "NORMAL"
        if any(abs(x - 230) > 23 for x in v) or pf < 0.9:  # ±10 % напруги (ДСТУ EN 50160) або низький cos φ
            status = "WARNING"
        packet: Dict[str, Any] = {
            "device_id": self.device_id,
            "sensor_type": self.sensor_type,
            "firmware": "sm-fw-3.2.1",
            "timestamp": ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "sequence_id": self.seq,
            "interval_minutes": self.interval,
            "measurements": {
                "voltage_v": {"L1": v[0], "L2": v[1], "L3": v[2]},
                "current_a": {f"L{i}": round(power_kw * 1000 / 3 / v[i - 1] / pf, 3) for i in (1, 2, 3)},
                "active_power_kw": power_kw,
                "reactive_power_kvar": round(power_kw * math.tan(math.acos(pf)), 3),
                "power_factor": pf,
                "frequency_hz": round(50 + self.rng.uniform(-0.05, 0.05), 3),
                "energy_import_kwh": self.energy_kwh,
                "thd_voltage_pct": round(self.rng.uniform(1.2, 4.5), 2),
            },
            "harmonics_v_pct": [round(self.rng.uniform(0.1, 3.0) / n, 3) for n in range(2, 16)],
            "load_profile_kw_1min": profile,
            "system_status": status,
        }
        # Доповнюємо службовим полем до цільового розміру пакета (≈2 КБ за варіантом)
        size = len(json.dumps(packet).encode())
        if size < self.packet_size:
            packet["reserved"] = "0" * max(0, self.packet_size - size - len(', "reserved": ""'))
        return packet

    def generate_telemetry_batch(self, count: int = 20, start: datetime | None = None) -> List[Dict[str, Any]]:
        start = start or datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=self.interval * count)
        return [self.generate_packet(start + timedelta(minutes=self.interval * i)) for i in range(count)]

    @staticmethod
    def encode(data: Dict[str, Any]) -> bytes:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def upload_via_presigned_url(self, presigned_url: str, data: Dict[str, Any], timeout: int = 15) -> Tuple[bool, int, Dict[str, str], float]:
        """HTTP PUT без облікових даних. Повертає (успіх, HTTP-код, заголовки, тривалість, с)."""
        body = self.encode(data)
        t0 = time.time()
        try:
            r = requests.put(presigned_url, data=body, headers={"Content-Type": "application/json"}, timeout=timeout)
            return r.status_code == 200, r.status_code, dict(r.headers), time.time() - t0
        except requests.RequestException as e:
            return False, 0, {"error": str(e)}, time.time() - t0

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
