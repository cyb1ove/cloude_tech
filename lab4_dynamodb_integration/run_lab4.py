"""
ЛБ4 · Варіант 3 · DynamoDB для телеметрії роботизованих маніпуляторів: схема, GSI,
Conditional Writes і бенчмарк Scan vs Query.

    python3 run_lab4.py                 # AWS: створити → завантажити 1000 → тести → бенчмарк → видалити таблицю
    python3 run_lab4.py --keep          # не видаляти таблицю (для перевірок AWS CLI)
    python3 run_lab4.py --localstack    # LocalStack
    python3 run_lab4.py --destroy [--localstack]   # видалити таблицю з output/benchmark_report.json
"""

import argparse
import json
import os
import sys
from decimal import Decimal

if sys.version_info < (3, 10):
    sys.exit("Потрібен Python 3.10+. На macOS: bash run_mac.sh")
try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Не знайдено залежностей: bash run_mac.sh")

from scripts import telemetry_benchmark as tb
from scripts.dynamodb_manager import DynamoDBManager, log

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join("config", "dynamodb_schema.json")
REPORT = os.path.join("output", "benchmark_report.json")
SAMPLES = os.path.join("output", "query_samples.json")
CHART = os.path.join("output", "benchmark_chart.png")


def banner(t: str) -> None:
    print("\n" + "=" * 65 + f"\n  {t}\n" + "=" * 65)


def jsonable(o):
    if isinstance(o, Decimal):
        return float(o) if o % 1 else int(o)
    raise TypeError(type(o))


def main() -> None:
    ap = argparse.ArgumentParser(description="ЛБ4 · варіант 3 · DynamoDB cps-robot-arms")
    ap.add_argument("--localstack", action="store_true")
    ap.add_argument("--keep", action="store_true", help="не видаляти таблицю наприкінці")
    ap.add_argument("--destroy", action="store_true")
    args = ap.parse_args()
    os.chdir(ROOT)
    os.makedirs("output", exist_ok=True)
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

    if args.destroy:
        with open(REPORT, encoding="utf-8") as f:
            DynamoDBManager(CONFIG, args.localstack, table_name=json.load(f)["table"]["TableName"]).delete_table()
        return

    banner("ПРОЄКТУВАННЯ ТА БЕНЧМАРКІНГ ХМАРНОЇ NoSQL СУБД (DYNAMODB)")
    if args.localstack:
        log("INFO", "Режим LocalStack: DynamoDB API -> http://localhost:4566")
    db = DynamoDBManager(CONFIG, localstack=args.localstack)
    report = {"backend": "LocalStack" if args.localstack else "AWS"}
    try:
        report["create_seconds"] = round(db.create_table_with_gsi(), 2)
        report["table"] = db.describe()
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=jsonable)
        d = report["table"]
        print(tabulate([
            ["Partition Key (HASH)", f"{db.pk} (S)"], ["Sort Key (RANGE)", f"{db.sk} (N)"],
            ["GSI", f"{d['GSI'][0]['IndexName']}: {d['GSI'][0]['KeySchema'][0]['AttributeName']} (S) + "
                    f"{d['GSI'][0]['KeySchema'][1]['AttributeName']} (N), проєкція {d['GSI'][0]['Projection']}"],
            ["Режим оплати", d["BillingMode"]], ["Шифрування", d.get("SSE") or "ENABLED"],
            ["TTL", cfg.get("ttl_attribute")]], tablefmt="fancy_grid"))

        # ------------------------------------------------ завантаження
        print("\n--- Генерація та завантаження тестового масиву телеметрії ---")
        items = tb.generate_dataset(cfg)
        report["load_seconds"] = round(db.batch_write_telemetry(items), 3)
        dist = {s: sum(1 for i in items if i["OperationalState"] == s) for s in cfg["dataset"]["state_weights"]}
        log("INFO", "Розподіл станів: " + ", ".join(f"{k}={v}" for k, v in dist.items()))

        # ------------------------------------------------ умовні записи
        print("\n--- Демонстрація механізму Conditional Writes ---")
        bench = cfg["benchmark"]
        robot = bench["query_robot"]
        target = max(i["Timestamp"] for i in items if i["RobotID"] == robot)
        log("INFO", f"Два оператори прочитали запис ({robot}, {target}) з Version=1.")
        ok1, v1 = db.conditional_update(robot, target, 1, {"OperationalState": "MOVING", "Operator": "operator-A"})
        log("SUCCESS" if ok1 else "ERROR", f"Оператор A: умовне оновлення успішно виконано (нова версія: {v1}).")
        ok2, _ = db.conditional_update(robot, target, 1, {"OperationalState": "IDLE", "Operator": "operator-B"})
        if not ok2:
            log("WARN", "Оператор B (очікував Version=1): Конфлікт версій! Спрацювало оптимістичне блокування "
                        "(ConditionalCheckFailedException) — зміни A не втрачено.")
        cur = db.get_item(robot, target)
        ok3, v3 = db.conditional_update(robot, target, int(cur["Version"]), {"OperationalState": "IDLE", "Operator": "operator-B"})
        log("SUCCESS" if ok3 else "ERROR", f"Оператор B перечитав запис (Version={cur['Version']}) і повторив: версія {v3}.")
        dup = db.put_if_absent(dict(items[0]))
        log("INFO" if not dup else "WARN", f"Повторна вставка існуючого ключа з attribute_not_exists: "
                                           f"{'відхилено (ідемпотентність)' if not dup else 'ПРИЙНЯТО'}")
        report["conditional_writes"] = {"first_update_version": v1, "stale_update_rejected": not ok2,
                                        "retry_version": v3, "duplicate_insert_rejected": not dup}

        # ------------------------------------------------ бенчмарк
        print(f"\n--- Проведення порівняльного бенчмаркінгу операцій ({bench['repetitions']} повторів + прогрів) ---")
        end_ts = target
        start_ts = end_ts - bench["query_window_minutes"] * 60 + 1
        ops = [
            ("Query PK (eventual)", lambda: db.query_telemetry_by_device_range(robot, start_ts, end_ts, False)),
            ("Query PK (strong)", lambda: db.query_telemetry_by_device_range(robot, start_ts, end_ts, True)),
            ("Query GSI ERROR ≥13.5 Н·м", lambda: db.query_gsi_by_state_torque(bench["scan_state"], bench["gsi_torque_threshold_nm"])),
            ("Scan + Filter ERROR", lambda: db.scan_telemetry_by_status(bench["scan_state"])),
        ]
        results = [tb.run_benchmark(n, fn, bench["repetitions"]) for n, fn in ops]
        print(tabulate([[r["operation"], r["count"], r["scanned"], f"{r['efficiency_pct']} %", r["pages"],
                         f"{r['latency_ms_median']} мс", f"{r['latency_ms_p95']} мс", r["rcu"]] for r in results],
                       headers=["Тип операції", "Count", "Scanned", "Count/Scanned", "Сторінок",
                                "Latency (медіана)", "p95", "RCU"], tablefmt="fancy_grid"))
        q, s = results[0], results[3]
        if q["latency_ms_median"]:
            log("INFO", f"Scan прочитав у {s['scanned'] / max(q['scanned'], 1):.0f} разів більше елементів і був у "
                        f"{s['latency_ms_median'] / q['latency_ms_median']:.1f} раза повільнішим за Query.")
        if not any(r["rcu"] for r in results):
            log("WARN", "Емулятор не повертає ConsumedCapacity — RCU див. у розрахунку нижче.")

        # ------------------------------------------------ розрахунок ємності
        banner("РОЗРАХУНОК WCU / RCU")
        calc = tb.capacity_calc(items, q["count"], s["scanned"])
        cost = tb.capacity_cost_model(cfg, calc["avg_item_bytes"], calc["query_rcu_eventual"])
        print(tabulate([
            ["Середній / макс. розмір елемента", f"{calc['avg_item_bytes']} / {calc['max_item_bytes']} Б"],
            ["WCU на елемент (⌈розмір/1 КБ⌉)", calc["wcu_per_item"]],
            ["WCU на завантаження 1000 елементів (+ стільки ж у GSI)", calc["wcu_total_load"]],
            ["Query: обсяг / RCU strong / RCU eventual", f"{calc['query_kb']} КБ / {calc['query_rcu_strong']} / {calc['query_rcu_eventual']}"],
            ["Scan: обсяг / RCU strong / RCU eventual", f"{calc['scan_kb']} КБ / {calc['scan_rcu_strong']} / {calc['scan_rcu_eventual']}"],
            [f"Парк {cfg['capacity_model']['robots']} роботів × {cfg['capacity_model']['sample_rate_hz']} Гц: On-Demand, $/міс",
             cost["on_demand_month_usd"]],
            ["Provisioned під пік (×%g), $/міс" % cfg["capacity_model"]["peak_to_average"], cost["provisioned_for_peak_month_usd"]],
            ["Provisioned + auto scaling 70 %, $/міс", cost["provisioned_autoscaling_70pct_month_usd"]],
            ["Беззбитковість On-Demand (утилізація піку)", f"{cost['on_demand_breakeven_utilization_pct']} % (фактично {cost['actual_utilization_pct']} %)"],
        ], tablefmt="fancy_grid"))

        chart = tb.plot_benchmark(results, CHART)
        samples = {"query_pk": db.query_telemetry_by_device_range(robot, start_ts, end_ts)["items"][:5],
                   "query_gsi": db.query_gsi_by_state_torque(bench["scan_state"], bench["gsi_torque_threshold_nm"])["items"][:5],
                   "scan_filter": db.scan_telemetry_by_status(bench["scan_state"])["items"][:5],
                   "updated_item": db.get_item(robot, target)}
        with open(SAMPLES, "w", encoding="utf-8") as f:
            json.dump(samples, f, indent=2, ensure_ascii=False, default=jsonable)
        report.update({"dataset": {"items": len(items), "state_distribution": dist}, "benchmark": results,
                       "capacity_calc": calc, "capacity_cost_model": cost,
                       "query_window": {"robot": robot, "start": start_ts, "end": end_ts}})
        report["table"] = db.describe()
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=jsonable)
        log("INFO", f"Звіти збережено у: output/ (benchmark_report.json, query_samples.json"
                    f"{', benchmark_chart.png' if chart else ''})")
        if args.keep:
            log("INFO", f"Таблицю залишено. Видалення: python3 run_lab4.py --destroy{' --localstack' if args.localstack else ''}")
        else:
            db.delete_table()
    except Exception as ex:  # noqa: BLE001
        log("ERROR", f"{type(ex).__name__}: {ex}")
        try:
            db.delete_table()
        except Exception:  # noqa: BLE001
            log("WARN", f"Видаліть таблицю вручну: python3 run_lab4.py --destroy")
        sys.exit(1)


if __name__ == "__main__":
    main()
