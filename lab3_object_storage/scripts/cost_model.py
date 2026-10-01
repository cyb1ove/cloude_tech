"""
Модель вартості зберігання телеметрії за формулою методичних вказівок:

    C_storage = Σ_k ( V_k·P_cap,k + N_put,k·P_put,k + N_get,k·P_get,k + V_ret,k·P_ret,k ),
    k ∈ {STANDARD, STANDARD_IA, GLACIER, DEEP_ARCHIVE}

Порівнюються два сценарії для парку розумних лічильників (варіант 3):
  A «як є»   — кожен 15-хвилинний пакет ≈2 КБ є окремим об'єктом. З вересня 2024 р. AWS за
               замовчуванням НЕ переносить об'єкти < 128 КБ у інші класи (TransitionDefaultMinimumObjectSize),
               а Standard-IA тарифікує мінімум 128 КБ на об'єкт — тож правила життєвого циклу для таких
               об'єктів не спрацюють і дані лежать у STANDARD до видалення (730 д).
  B «агрегація в хмарі» — пристрої завантажують пакети в raw/ (зберігаються 2 доби), щоденне
               завдання об'єднує 96 пакетів пристрою в один об'єкт ≈192 КБ у telemetry/, до якого правила
               STANDARD → IA (60 д) → GLACIER (120 д) → видалення (730 д) застосовуються повністю.
  C «пакетування на шлюзі» — шлюз/лічильник буферизує пакети й раз на добу виконує один PUT
               об'єкта ≈192 КБ (через presigned URL); lifecycle застосовується повністю.
Місяць = 30 діб; когорта місяця m має середній вік (M − m)·30 + 15 діб.
"""

from typing import Any, Dict, List

GB_PER_KB = 1 / (1024 * 1024)


def _tier(age_days: float, lc: Dict[str, Any]) -> str | None:
    if age_days >= lc["expiration_days"]:
        return None
    if age_days >= lc["transition_glacier_days"]:
        return "GLACIER"
    if age_days >= lc["transition_ia_days"]:
        return "STANDARD_IA"
    return "STANDARD"


def _aggregated(m: int, cm: Dict[str, Any], lc: Dict[str, Any], p: Dict[str, float], agg_kb: float,
                agg_objects_month: int, pkts_month: int, prev: Dict[int, str | None],
                raw_gb: float, ingest_raw: bool) -> Dict[str, Any]:
    """Місяць m для сценаріїв з об'єктами ≈192 КБ (lifecycle застосовується)."""
    vol = {"STANDARD": raw_gb, "STANDARD_IA": 0.0, "GLACIER": 0.0}
    n_ia = n_gl = 0
    for c in range(1, m + 1):
        age = (m - c) * 30 + 15
        tier = _tier(age, lc)
        if tier == "STANDARD_IA" and prev.get(c) != "STANDARD_IA":
            n_ia += agg_objects_month
        if tier == "GLACIER" and prev.get(c) != "GLACIER":
            n_gl += agg_objects_month
        prev[c] = tier
        if tier is None:
            continue
        size_kb = agg_kb
        if tier == "STANDARD_IA":
            size_kb = max(agg_kb, cm["ia_min_billable_kb"])
        if tier == "GLACIER":  # 32 КБ індексу за тарифом GLACIER + 8 КБ метаданих за тарифом STANDARD
            vol["STANDARD"] += agg_objects_month * 8 * GB_PER_KB
            size_kb = agg_kb + cm["glacier_overhead_kb"] - 8
        vol[tier] += agg_objects_month * size_kb * GB_PER_KB
    storage = sum(vol[k] * p[k] for k in vol)
    puts = agg_objects_month + (pkts_month if ingest_raw else 0)
    put = puts / 1000 * p["put_per_1000"]
    get = (pkts_month / 1000 * p["get_per_1000"]) if ingest_raw else 0.0
    trans = n_ia / 1000 * p["transition_ia_per_1000"] + n_gl / 1000 * p["transition_glacier_per_1000"]
    return {"volume_gb": {k: round(v, 3) for k, v in vol.items()}, "storage": round(storage, 4), "put": round(put, 4),
            "get": round(get, 4), "transitions": round(trans, 4), "total": round(storage + put + get + trans, 4)}


def simulate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cm, lc = cfg["cost_model"], cfg["lifecycle_rules"]
    p = cm["prices_usd"]
    devices, months = cm["devices"], cm["months"]
    pkt_kb = cfg["sensor"]["packet_size_bytes"] / 1024
    per_day = 24 * 60 // cm["interval_minutes"]
    pkts_month = devices * per_day * 30

    agg_kb = per_day * pkt_kb                       # один об'єкт на пристрій на добу
    agg_objects_month = devices * 30
    transition_min = cm.get("transition_min_object_kb", 128)

    rows: List[Dict[str, Any]] = []
    prev_b: Dict[int, str | None] = {}
    prev_c: Dict[int, str | None] = {}
    for m in range(1, months + 1):
        # ---------- сценарій A
        vol_a = {"STANDARD": 0.0, "STANDARD_IA": 0.0, "GLACIER": 0.0}
        for c in range(1, m + 1):
            age = (m - c) * 30 + 15
            tier = _tier(age, lc)
            if tier is None:
                continue
            if pkt_kb < transition_min:
                tier = "STANDARD"                  # перехід не виконується (об'єкт < 128 КБ)
            vol_a[tier] += pkts_month * pkt_kb * GB_PER_KB
        cost_a_storage = sum(vol_a[k] * p[k] for k in vol_a)
        cost_a_put = pkts_month / 1000 * p["put_per_1000"]
        total_a = cost_a_storage + cost_a_put

        res_b = _aggregated(m, cm, lc, p, agg_kb, agg_objects_month, pkts_month, prev_b,
                            raw_gb=devices * per_day * 2 * pkt_kb * GB_PER_KB, ingest_raw=True)
        res_c = _aggregated(m, cm, lc, p, agg_kb, agg_objects_month, pkts_month, prev_c,
                            raw_gb=0.0, ingest_raw=False)

        rows.append({
            "month": m,
            "A": {"volume_gb": {k: round(v, 3) for k, v in vol_a.items()}, "storage": round(cost_a_storage, 4),
                  "put": round(cost_a_put, 4), "total": round(total_a, 4)},
            "B": res_b,
            "C": res_c,
        })

    def year(sc: str, y: int) -> float:
        return round(sum(r[sc]["total"] for r in rows if (y - 1) * 12 < r["month"] <= y * 12), 2)

    return {
        "assumptions": {"devices": devices, "interval_minutes": cm["interval_minutes"], "packets_per_month": pkts_month,
                        "packet_kb": round(pkt_kb, 2), "aggregate_kb": round(agg_kb, 1),
                        "transition_min_object_kb": transition_min, "prices_usd": p},
        "monthly": rows,
        "summary": {
            "A_year1": year("A", 1), "A_year2": year("A", 2),
            "B_year1": year("B", 1), "B_year2": year("B", 2),
            "C_year1": year("C", 1), "C_year2": year("C", 2),
            "A_month24": rows[-1]["A"], "B_month24": rows[-1]["B"], "C_month24": rows[-1]["C"],
        },
    }


def per_run_cost(n_put: int, n_get: int, total_bytes: int, p: Dict[str, float]) -> Dict[str, float]:
    """Вартість запусків лабораторної (для порівняння з масштабом парку)."""
    return {"put": round(n_put / 1000 * p["put_per_1000"], 6), "get": round(n_get / 1000 * p["get_per_1000"], 6),
            "storage_month": round(total_bytes / 1024 ** 3 * p["STANDARD"], 8)}
