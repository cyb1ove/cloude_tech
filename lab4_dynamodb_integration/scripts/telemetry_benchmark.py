"""
Генерація телеметрії маніпуляторів, розрахунок RCU/WCU і порівняльний бенчмарк Scan vs Query.

Модель даних (варіант 3): момент у суглобі JointTorqueNm (Н·м) і кутова швидкість
AngularVelocityRadS (рад/с) для станів IDLE / MOVING / ERROR.
"""

import math
import random
import statistics
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List

# ------------------------------------------------------------ генерація даних
STATE_PROFILE = {
    # стан: (момент, Н·м), (кутова швидкість, рад/с), (температура двигуна, °C)
    "IDLE":   ((0.5, 2.0),  (0.0, 0.1),  (32, 40)),
    "MOVING": ((3.0, 11.5), (0.5, 3.5),  (40, 58)),
    "ERROR":  ((12.0, 15.0), (0.0, 0.3), (60, 78)),   # перевантаження/заклинювання суглоба
}


def D(x: float, nd: int = 3) -> Decimal:
    """DynamoDB через boto3 приймає числа лише як Decimal (без втрати точності float)."""
    return Decimal(str(round(x, nd)))


def generate_dataset(cfg: Dict[str, Any], now: int | None = None) -> List[Dict[str, Any]]:
    ds = cfg["dataset"]
    rng = random.Random(ds["seed"])
    now = now or int(time.time())
    states, weights = zip(*ds["state_weights"].items())
    ttl = now + cfg.get("ttl_days", 30) * 86400
    items = []
    for r in range(1, ds["robots"] + 1):
        robot = f"ROBOT-{r:02d}"
        for i in range(ds["readings_per_robot"]):
            ts = now - (ds["readings_per_robot"] - 1 - i) * ds["interval_seconds"]
            state = rng.choices(states, weights)[0]
            (t0, t1), (w0, w1), (c0, c1) = STATE_PROFILE[state]
            items.append({
                "RobotID": robot,
                "Timestamp": ts,
                "OperationalState": state,
                "JointTorqueNm": D(rng.uniform(t0, t1)),
                "AngularVelocityRadS": D(rng.uniform(w0, w1)),
                "JointID": f"J{rng.randint(1, 6)}",
                "MotorTempC": D(rng.uniform(c0, c1), 1),
                "Version": 1,
                "ExpiresAt": ttl,
            })
    return items


# ------------------------------------------------------------ розмір і ємність
def item_size_bytes(item: Dict[str, Any]) -> int:
    """Оцінка розміру елемента за правилами DynamoDB: ім'я атрибута + значення.
    Рядок — довжина UTF-8; число — (кількість значущих цифр / 2) + 1 байт."""
    size = 0
    for name, val in item.items():
        size += len(name.encode())
        if isinstance(val, str):
            size += len(val.encode())
        elif isinstance(val, (int, Decimal, float)):
            digits = len(str(abs(val)).replace(".", "").lstrip("0")) or 1
            size += math.ceil(digits / 2) + 1
        elif isinstance(val, bool):
            size += 1
        else:
            size += len(str(val))
    return size


def capacity_calc(items: List[Dict[str, Any]], query_count: int, scanned_all: int) -> Dict[str, Any]:
    sizes = [item_size_bytes(i) for i in items]
    avg = sum(sizes) / len(sizes)
    wcu_per_item = math.ceil(max(sizes) / 1024)
    q_kb = query_count * avg / 1024
    s_kb = scanned_all * avg / 1024
    return {
        "avg_item_bytes": round(avg, 1), "max_item_bytes": max(sizes),
        "wcu_per_item": wcu_per_item, "wcu_total_load": wcu_per_item * len(items),
        "query_kb": round(q_kb, 2),
        "query_rcu_strong": math.ceil(q_kb / 4), "query_rcu_eventual": math.ceil(q_kb / 4) / 2,
        "scan_kb": round(s_kb, 2),
        "scan_rcu_strong": math.ceil(s_kb / 4), "scan_rcu_eventual": math.ceil(s_kb / 4) / 2,
        "gsi_write_amplification": "кожен запис у таблицю = +1 запис у GSI (проєкція ALL)",
    }


def capacity_cost_model(cfg: Dict[str, Any], avg_item_bytes: float, query_rcu: float) -> Dict[str, Any]:
    """Provisioned vs On-Demand для реального навантаження парку маніпуляторів."""
    cm = cfg["capacity_model"]
    p = cm["prices_usd_eu_central_1"]
    hours = 730
    writes_s = cm["robots"] * cm["sample_rate_hz"]
    wcu_item = math.ceil(avg_item_bytes / 1024)
    avg_wcu = writes_s * wcu_item * 2                          # ×2: запис у таблицю + у GSI
    avg_rcu = cm["dashboard_queries_per_minute"] / 60 * query_rcu
    peak = cm["peak_to_average"]
    on_demand = (avg_wcu * 3600 * hours / 1e6 * p["on_demand_write_per_million"] +
                 avg_rcu * 3600 * hours / 1e6 * p["on_demand_read_per_million"])
    prov_peak = (math.ceil(avg_wcu * peak) * p["provisioned_wcu_hour"] + math.ceil(max(avg_rcu * peak, 1)) *
                 p["provisioned_rcu_hour"]) * hours
    prov_auto = (math.ceil(avg_wcu / 0.7) * p["provisioned_wcu_hour"] + math.ceil(max(avg_rcu / 0.7, 1)) *
                 p["provisioned_rcu_hour"]) * hours           # auto scaling з цільовою утилізацією 70 %
    # Беззбитковість: середня утилізація u = середнє/пік, за якої On-Demand коштує стільки ж,
    # скільки Provisioned, виділений під пік: OD(пік)·u = Prov(пік)  →  u* = Prov / OD(пік)
    breakeven = 100 * prov_peak / (on_demand * peak) if on_demand else None
    return {
        "writes_per_second": writes_s, "avg_wcu": avg_wcu, "avg_rcu": round(avg_rcu, 3),
        "on_demand_month_usd": round(on_demand, 2),
        "provisioned_for_peak_month_usd": round(prov_peak, 2),
        "provisioned_autoscaling_70pct_month_usd": round(prov_auto, 2),
        "actual_utilization_pct": round(100 / peak, 1),
        "on_demand_breakeven_utilization_pct": round(breakeven, 1) if breakeven else None,
    }


# ------------------------------------------------------------ бенчмарк
def run_benchmark(name: str, fn: Callable[[], Dict[str, Any]], repetitions: int) -> Dict[str, Any]:
    fn()  # прогрів: встановлення TCP/TLS-з'єднання, кеш метаданих
    runs = [fn() for _ in range(repetitions)]
    lat = sorted(r["latency_ms"] for r in runs)
    p95 = lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)]
    last = runs[-1]
    return {"operation": name, "count": last["count"], "scanned": last["scanned"], "pages": last["pages"],
            "rcu": last["rcu"], "latency_ms_median": round(statistics.median(lat), 2),
            "latency_ms_mean": round(statistics.fmean(lat), 2), "latency_ms_p95": round(p95, 2),
            "latency_ms_min": round(lat[0], 2), "repetitions": repetitions,
            "efficiency_pct": round(100 * last["count"] / last["scanned"], 1) if last["scanned"] else None}


def plot_benchmark(results: List[Dict[str, Any]], path: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    names = [r["operation"] for r in results]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    colors = ["#2e7d32", "#1565c0", "#6a1b9a", "#c62828"][:len(results)]
    for ax, key, title in zip(axes, ("latency_ms_median", "scanned", "rcu"),
                              ("Затримка (медіана), мс", "Прочитано елементів (ScannedCount)", "Спожито RCU")):
        vals = [r[key] or 0 for r in results]
        bars = ax.bar(range(len(vals)), vals, color=colors)
        ax.set_title(title, fontsize=10)
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(names, rotation=18, ha="right", fontsize=8)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{v:g}", ha="center", va="bottom", fontsize=8)
        ax.grid(axis="y", alpha=0.3)
    fig.suptitle("ЛБ4 · варіант 3 · Scan vs Query (cps-robot-arms)", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True
