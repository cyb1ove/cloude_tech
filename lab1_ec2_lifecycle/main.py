"""
ЛБ 1 · Варіант 3 · Оркестрація життєвого циклу вузла КФС "cps-sensor-aggregator".

Сценарій:
    1. Створення інстансу (AMI Debian 12, t3.micro, EBS gp3 15 ГБ, SG 22/1883).
    2. Очікування 'running' та проходження перевірок стану (status_ok).
    3. Збирання метаданих -> таблиця + output/instance_manifest.json.
    4. Функціональний тест Mosquitto (MQTT CONNECT -> CONNACK).
    5. Stop -> Start та повторний тест MQTT (сервіс має піднятися сам).
    6. Termination, розрахунок T_wait і C_total -> output/lifecycle_report.json.

Запуск:
    python3 main.py              # повний цикл
    python3 main.py --keep       # не знищувати інстанс (для скріншотів/SSH)
    python3 main.py --purge      # після termination видалити також SG та ключ
    python3 main.py --skip-mqtt  # без функціонального тесту брокера
    python3 main.py --localstack # емулятор LocalStack замість AWS (див. scripts/localstack.sh)
"""

import argparse
import json
import os
import sys

# macOS постачає /usr/bin/python3 3.9 (Xcode CLT), а методичка вимагає 3.10+.
if sys.version_info < (3, 10):
    sys.exit(f"Потрібен Python 3.10+, зараз {sys.version.split()[0]} ({sys.executable}).\n"
             "На macOS: brew install python@3.12  або запустіть  bash run_mac.sh")

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Не знайдено залежностей. Активуйте venv (source venv/bin/activate) "
             "і виконайте pip install -r requirements.txt, або запустіть bash run_mac.sh")

from scripts.ec2_manager import EC2LifecycleManager


def print_details(details: dict) -> None:
    """Виведення метаданих інстансу у вигляді таблиць."""
    skip = {"Tags", "RootVolume"}
    rows = [[k, ", ".join(v) if isinstance(v, list) else v]
            for k, v in details.items() if k not in skip]
    print("\n" + tabulate(rows, headers=["Параметр", "Значення"], tablefmt="fancy_grid"))

    if details.get("RootVolume"):
        print("\nКореневий том EBS:")
        print(tabulate([[k, v] for k, v in details["RootVolume"].items()],
                       headers=["Атрибут", "Значення"], tablefmt="grid"))

    print("\nПризначені теги інстансу:")
    print(tabulate([[k, v] for k, v in details["Tags"].items()],
                   headers=["Ключ тегу", "Значення"], tablefmt="grid"))


def print_report(report: dict) -> None:
    """Підсумкова таблиця часу очікування та вартості."""
    rows = [[event, f"{sec:.1f}"] for event, sec in report["waits"].items()]
    rows.append(["СУМАРНИЙ T_wait", f"{report['total_wait_seconds']:.1f}"])
    print("\n" + tabulate(rows, headers=["Етап очікування", "Секунди"], tablefmt="grid"))

    cost = [
        ["t_run (стан running), с", report["t_run_seconds"]],
        ["t_life (існування тому EBS), с", report["t_life_seconds"]],
        ["P_compute, $/год", report["P_compute_usd_per_hour"]],
        ["P_storage, $/ГБ·міс", report["P_storage_usd_per_gb_month"]],
        ["V_EBS, ГБ", report["V_EBS_gb"]],
        ["Обчислення, $", f"{report['compute_cost_usd']:.6f}"],
        ["Сховище, $", f"{report['storage_cost_usd']:.6f}"],
        ["C_total за сеанс, $", f"{report['C_total_usd']:.6f}"],
        ["Оцінка 24/7 за місяць (730 год), $", report["monthly_24x7_estimate_usd"]],
    ]
    print(tabulate(cost, headers=["Показник", "Значення"], tablefmt="grid",
                   disable_numparse=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="ЛБ1 · Варіант 3 · cps-sensor-aggregator")
    parser.add_argument("--keep", action="store_true", help="не знищувати інстанс наприкінці")
    parser.add_argument("--purge", action="store_true", help="видалити SG та ключ після termination")
    parser.add_argument("--skip-mqtt", action="store_true", help="пропустити тест MQTT")
    parser.add_argument("--localstack", action="store_true",
                        help="працювати з LocalStack (http://localhost:4566) замість AWS")
    args = parser.parse_args()

    # Відносні шляхи (config/, scripts/, output/) рахуються від теки проєкту,
    # тож main.py можна запускати з будь-якого каталогу (напр. з Finder/iTerm).
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print("=================================================================")
    print("  АВТОМАТИЗАЦІЯ ЖИТТЄВОГО ЦИКЛУ IaaS ІНСТАНСУ AWS EC2 (BOTO3)")
    print("  Варіант 3 · cps-sensor-aggregator · Debian 12 · Mosquitto MQTT")
    print("=================================================================\n")

    config_file = os.path.join("config", "settings.json")
    user_data_file = os.path.join("scripts", "user_data.sh")
    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)
    manifest_file = os.path.join(output_dir, "instance_manifest.json")
    report_file = os.path.join(output_dir, "lifecycle_report.json")

    manager = EC2LifecycleManager(config_path=config_file, localstack=args.localstack)

    try:
        # 1. Створення та запуск інстансу (стан pending)
        manager.provision_instance(user_data_script_path=user_data_file)

        # 2. pending -> running, далі проходження системних перевірок EC2
        manager.wait_for_state("running")
        if manager.localstack:
            # Контейнер-«інстанс» не проходить системні перевірки EC2 — пропускаємо
            print("[INFO] LocalStack: перевірку instance_status_ok пропущено.")
        else:
            manager.wait_for_state("status_ok", delay=15, max_attempts=40)

        # 3. Метадані та маніфест
        details = manager.get_instance_details()
        print_details(details)
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(details, f, indent=4, ensure_ascii=False)
        print(f"\n[INFO] Повний маніфест інстансу збережено у: {manifest_file}")

        # 4. Функціональний тест брокера (cloud-init може ще встановлювати пакети)
        if not args.skip_mqtt:
            _mqtt_check(manager, details, attempts=30)

        # 5. Демонстрація переходів станів
        print("\n--- Демонстрація переходу станів (Stop / Start) ---")
        manager.stop_instance()
        manager.start_instance()
        details = manager.get_instance_details()
        print(f"[INFO] Адреса після перезапуску: {details['PublicIpAddress']} "
              f"(в AWS без Elastic IP публічна IP змінюється)")
        details["Note"] = "Маніфест оновлено після циклу stop/start"
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(details, f, indent=4, ensure_ascii=False)
        if not args.skip_mqtt:
            _mqtt_check(manager, details, attempts=18)

        # 6. Утилізація
        if args.keep:
            print(f"\n[INFO] --keep: інстанс {manager.instance_id} залишено запущеним. "
                  f"Не забудьте видалити його: aws ec2 terminate-instances "
                  f"--instance-ids {manager.instance_id}")
        else:
            print("\n--- Завершення лабораторної роботи та утилізація ресурсів ---")
            manager.terminate_instance()
            if args.purge:
                manager.cleanup_network_and_keys()

    except KeyboardInterrupt:
        print("\n[WARN] Перервано користувачем.")
        _emergency_cleanup(manager, args.keep)
    except Exception as ex:  # noqa: BLE001
        print(f"\n[ERROR] Виникла виключна ситуація: {ex}")
        _emergency_cleanup(manager, args.keep)
    finally:
        # Аналітика формується навіть після помилки — для розділу висновків звіту
        if manager.timeline:
            report = manager.lifecycle_report()
            print_report(report)
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=4, ensure_ascii=False)
            print(f"[INFO] Звіт про таймінги та вартість збережено у: {report_file}")


def _mqtt_check(manager: EC2LifecycleManager, details: dict, attempts: int) -> None:
    """Функціональний тест брокера за адресою з AWS або з пробросу портів LocalStack."""
    target = manager.mqtt_target(details)
    if not target:
        print("[WARN] Адресу брокера не визначено"
              + (" (не знайдено Docker-контейнер інстансу)" if manager.localstack else "")
              + " — тест MQTT пропущено.")
        return
    manager.wait_for_mqtt(target[0], target[1], attempts=attempts)


def _emergency_cleanup(manager: EC2LifecycleManager, keep: bool) -> None:
    """Аварійне знищення інстансу, щоб не залишити платний ресурс."""
    if manager.instance_id and not keep:
        print(f"[CLEANUP] Спроба екстреного знищення інстансу {manager.instance_id}...")
        try:
            manager.terminate_instance()
        except Exception as clean_err:  # noqa: BLE001
            print(f"[CLEANUP ERROR] Не вдалося видалити інстанс: {clean_err}")


if __name__ == "__main__":
    main()
