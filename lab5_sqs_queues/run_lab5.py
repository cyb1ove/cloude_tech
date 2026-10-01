"""
ЛБ5 · Варіант 3 · Асинхронна черга телеметрії маніпуляторів SQS + Dead-Letter Queue.

    python3 run_lab5.py                 # AWS: черги → 1000 повідомлень → простій → 8 обробників → DLQ → звіт → видалення черг
    python3 run_lab5.py --keep          # залишити черги (перевірка AWS CLI), потім --destroy
    python3 run_lab5.py --redrive       # наприкінці повернути повідомлення з DLQ у основну чергу (StartMessageMoveTask)
    python3 run_lab5.py --localstack    # LocalStack
"""

import argparse
import json
import math
import os
import sys
import time

if sys.version_info < (3, 10):
    sys.exit("Потрібен Python 3.10+. На macOS: bash run_mac.sh")
try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Не знайдено залежностей: bash run_mac.sh")

from scripts.sqs_queue_manager import SQSQueueManager, log
from scripts.telemetry_consumer import TelemetryConsumerPool
from scripts.telemetry_producer import TelemetryProducer

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join("config", "sqs_config.json")
REPORT = os.path.join("output", "queue_benchmark_report.json")
DLQ_REPORT = os.path.join("output", "dlq_analysis.json")


def banner(t: str) -> None:
    print("\n" + "=" * 65 + f"\n  {t}\n" + "=" * 65)


def measure_rtt(mgr: SQSQueueManager, n: int = 5) -> float:
    t = []
    for _ in range(n):
        t0 = time.perf_counter()
        mgr.get_queue_metrics(mgr.main_url)
        t.append((time.perf_counter() - t0) * 1000)
    return round(sorted(t)[len(t) // 2], 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="ЛБ5 · варіант 3 · SQS cps-robot-telemetry + DLQ")
    ap.add_argument("--localstack", action="store_true")
    ap.add_argument("--keep", action="store_true", help="не видаляти черги наприкінці")
    ap.add_argument("--redrive", action="store_true", help="повернути повідомлення з DLQ (StartMessageMoveTask)")
    ap.add_argument("--destroy", action="store_true", help="видалити черги з output/queue_benchmark_report.json")
    args = ap.parse_args()
    os.chdir(ROOT)
    os.makedirs("output", exist_ok=True)
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

    if args.destroy:
        with open(REPORT, encoding="utf-8") as f:
            q = json.load(f)["queues"]
        mgr = SQSQueueManager(cfg, args.localstack)
        mgr.main_url, mgr.dlq_url = q["main_url"], q["dlq_url"]
        mgr.delete_queues()
        return

    banner("АСИНХРОННА ОБРОБКА ТЕЛЕМЕТРІЇ КФС ЧЕРГАМИ SQS ТА DEAD-LETTER QUEUE")
    if args.localstack:
        log("INFO", "Режим LocalStack: SQS API -> http://localhost:4566")
    mgr = SQSQueueManager(cfg, args.localstack)
    report = {"backend": "LocalStack" if args.localstack else "AWS", "variant": 3}
    try:
        main_url, dlq_url = mgr.setup_queues_with_dlq()
        report["queues"] = {"main": mgr.main_queue_name, "dlq": mgr.dlq_name, "main_url": main_url, "dlq_url": dlq_url,
                            "main_attributes": {k: v for k, v in mgr.describe(main_url).items()
                                                if k in ("VisibilityTimeout", "MessageRetentionPeriod", "RedrivePolicy",
                                                         "ReceiveMessageWaitTimeSeconds", "SqsManagedSseEnabled")}}
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        # ------------------------------------------------ продюсер
        print()
        prod = TelemetryProducer(mgr.sqs, main_url, cfg).send_all()
        poison = set(prod.pop("poison_ids"))

        # ------------------------------------------------ простій споживачів
        print(f"\n--- Імітація аварійного простою обробників ({cfg['outage_seconds']} с) ---")
        time.sleep(cfg["outage_seconds"])
        m = mgr.wait_metrics_settle(main_url, expected_total=prod["sent"])
        buffered = m.get("Available", 0) + m.get("InFlight", 0)
        log("AUDIT", f"Стан черги під час простою: {m.get('Available')} доступно, {m.get('InFlight')} в обробці "
                     f"→ буферизовано {buffered}/{prod['sent']} ({100 * buffered / prod['sent']:.1f} %), втрат 0 %")

        # ------------------------------------------------ обробка хвилями
        print()
        pool = TelemetryConsumerPool(mgr.new_client, main_url, cfg)
        waves = [pool.run_wave(1)]
        vt = cfg["visibility_timeout_seconds"]
        print(f"\n--- Очікування спрацювання Redrive Policy (maxReceiveCount={cfg['max_receive_count']}) ---")
        for w in range(2, cfg["max_waves"] + 1):
            main_m = mgr.get_queue_metrics(main_url)
            dlq_m = mgr.get_queue_metrics(dlq_url)
            if main_m["Available"] + main_m["InFlight"] == 0 and dlq_m["Available"] >= len(poison):
                break
            log("INFO", f"Пауза {vt + 2} с: VisibilityTimeout {vt} с + 2 с запасу (основна: {main_m}, DLQ: {dlq_m['Available']})")
            time.sleep(vt + 2)
            waves.append(pool.run_wave(w))

        main_final = mgr.wait_metrics_settle(main_url, expected_total=0)
        dlq_final = mgr.wait_metrics_settle(dlq_url, expected_total=len(poison))

        # ------------------------------------------------ аналіз DLQ
        dlq_msgs = mgr.peek_dlq(max_messages=len(poison) + 10)
        dlq_ids = {json.loads(x["Body"])["sequence_id"] for x in dlq_msgs}
        reasons = {}
        for x in dlq_msgs:
            body = json.loads(x["Body"])
            bad = [f"{j}={v}" for j, v in body["encoder_angles_rad"].items() if isinstance(v, str)]
            reasons[body["sequence_id"]] = {"robot": body["robot_id"], "corrupted": bad,
                                            "receive_count_in_dlq": x["Attributes"].get("ApproximateReceiveCount")}
        dlq_analysis = {"expected_poison": len(poison), "in_dlq": len(dlq_ids),
                        "all_poison_in_dlq": dlq_ids == poison, "unexpected_in_dlq": sorted(dlq_ids - poison),
                        "missing_from_dlq": sorted(poison - dlq_ids), "messages": reasons}
        with open(DLQ_REPORT, "w", encoding="utf-8") as f:
            json.dump(dlq_analysis, f, indent=2, ensure_ascii=False)

        # ------------------------------------------------ розрахунки
        valid_unique = len(pool.seen_sequences)
        remaining = main_final.get("Available", 0) + main_final.get("InFlight", 0)
        lost = prod["sent"] - valid_unique - len(dlq_ids) - remaining
        ps = pool.processing_stats()
        rtt = measure_rtt(mgr)
        w1 = waves[0]
        mus = [v["mu_msg_s"] for v in w1["per_worker"].values() if v["mu_msg_s"]]
        mu = round(sum(mus) / len(mus), 1) if mus else None
        k = cfg["consumer_threads"]
        t_vis = (ps.get("mean_ms", 0) + 3 * ps.get("stdev_ms", 0) + 2 * rtt) / 1000 if ps else None
        drain = {}
        if mu:
            for q_spike, lam in ((prod["sent"], 0), (10000, 50), (50000, 200)):
                cap = k * mu - lam
                drain[f"Q={q_spike}, λ={lam}"] = round(q_spike / cap, 1) if cap > 0 else "∞ (k·μ ≤ λ)"
        calc = {"processing": ps, "rtt_ms": rtt, "t_vis_min_s": round(t_vis, 3) if t_vis else None,
                "t_vis_configured_s": vt, "mu_per_worker_msg_s": mu, "k": k,
                "effective_throughput_wave1_msg_s": round((w1["valid"] + w1["poison_errors"]) / w1["seconds"], 1),
                "t_drain_s": drain,
                "min_workers_10000_in_60s_at_lambda_50": math.ceil((10000 / 60 + 50) / mu) if mu else None}

        banner("ПІДСУМКОВИЙ ЗВІТ БЕНЧМАРКІНГУ")
        print(tabulate([
            ["Згенеровано повідомлень", prod["sent"]],
            ["Впроваджено poison pill", len(poison)],
            ["Продуктивність продюсера", f"{prod['throughput_msg_s']} msg/s ({prod['api_calls']} викликів SendMessageBatch)"],
            ["Буферизовано під час простою обробників", f"{buffered} (100 %)" if buffered == prod["sent"] else buffered],
            ["Успішно опрацьовано коректних", f"{valid_unique} msg (дублікатів доставки: {sum(w['duplicates'] for w in waves)})"],
            ["Повідомлень, переміщених у DLQ", f"{len(dlq_ids)} ({'100 % poison, збіг sequence_id' if dlq_analysis['all_poison_in_dlq'] else 'розбіжність!'})"],
            ["Залишок в основній черзі", remaining],
            ["Хвиль обробки / помилок poison по хвилях", f"{len(waves)} / " + ", ".join(str(w['poison_errors']) for w in waves)],
            ["Рівень втрат даних", f"{100 * max(lost, 0) / prod['sent']:.2f} % " + ("(Повна надійність)" if lost == 0 else "")],
        ], tablefmt="fancy_grid"))
        print(tabulate([
            ["Час обробки: середнє / σ / p99", f"{ps.get('mean_ms')} / {ps.get('stdev_ms')} / {ps.get('p99_ms')} мс"],
            ["RTT запиту до SQS (медіана)", f"{rtt} мс"],
            ["T_vis ≥ t̄ + 3σ + 2·RTT", f"{calc['t_vis_min_s']} с (налаштовано {vt} с — запас ×{vt / calc['t_vis_min_s']:.0f})" if calc["t_vis_min_s"] else "—"],
            ["μ одного обробника / k", f"{mu} msg/s / {k}"],
            ["Пропускна здатність хвилі 1 (з очікуванням VisibilityTimeout)", f"{calc['effective_throughput_wave1_msg_s']} msg/s"],
        ] + [[f"T_drain {kk}", f"{vv} с"] for kk, vv in drain.items()], tablefmt="fancy_grid"))

        report.update({"producer": prod, "outage_audit": m, "waves": waves, "main_final": main_final,
                       "dlq_final": dlq_final, "valid_unique": valid_unique, "lost": lost,
                       "dlq_summary": {k2: v for k2, v in dlq_analysis.items() if k2 != "messages"}, "calculations": calc})

        if args.redrive:
            task = mgr.redrive_dlq()
            if task:
                log("INFO", f"StartMessageMoveTask запущено: {task}. Повідомлення повертаються в основну чергу.")
                time.sleep(5)
                report["redrive"] = {"task": task, "main_after": mgr.get_queue_metrics(main_url),
                                     "dlq_after": mgr.get_queue_metrics(dlq_url)}
                log("INFO", f"Після redrive: основна {report['redrive']['main_after']}, DLQ {report['redrive']['dlq_after']}")

        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        log("INFO", f"Звіти збережено: {REPORT}, {DLQ_REPORT}")
        if args.keep:
            log("INFO", f"Черги залишено. Видалення: python3 run_lab5.py --destroy{' --localstack' if args.localstack else ''}")
        else:
            mgr.delete_queues()
        log("SUCCESS", "Лабораторну роботу успішно виконано.")
    except Exception as ex:  # noqa: BLE001
        log("ERROR", f"{type(ex).__name__}: {ex}")
        mgr.delete_queues()
        sys.exit(1)


if __name__ == "__main__":
    main()
