"""
ЛБ2 · Варіант 3 · Розгортання ізольованої мережі VPC з Bastion Host
та приватним OPC UA Industrial Server (TCP 4840).

    python3 deploy_network.py                 # розгортання в AWS + діагностика
    python3 deploy_network.py --localstack    # control plane у LocalStack + data plane на Docker-стенді
    python3 deploy_network.py --skip-tests    # лише розгортання
    python3 deploy_network.py --no-nacl       # без власних Network ACL
    python3 deploy_network.py --diagnose      # повторити діагностику для наявної інфраструктури
    python3 deploy_network.py --destroy [--localstack]   # видалити все з output/network_state.json

УВАГА (AWS): NAT Gateway тарифікується погодинно (~0.052 $/год) — після
перевірок обов'язково виконайте --destroy.
"""

import argparse
import json
import os
import subprocess
import sys
import time

if sys.version_info < (3, 10):
    sys.exit(f"Потрібен Python 3.10+, зараз {sys.version.split()[0]}. На macOS: bash run_mac.sh")

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Не знайдено залежностей: source venv/bin/activate && pip install -r requirements.txt (або bash run_mac.sh)")

from scripts import network_diagnostics as diag
from scripts.vpc_builder import VPCNetworkBuilder, load_state

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join("config", "network_schema.json")
STATE = os.path.join("output", "network_state.json")
REPORT = os.path.join("output", "routing_report.txt")


def banner(title: str) -> None:
    print("=" * 65)
    print(f"  {title}")
    print("=" * 65)


def save_state(state: dict) -> None:
    os.makedirs("output", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4, ensure_ascii=False)


def print_summary(st: dict, cfg: dict) -> None:
    banner("РЕЗУЛЬТАТИ РОЗГОРТАННЯ VPC")
    rows = [
        ["Бекенд", st.get("backend")],
        ["VPC ID (CIDR: %s)" % cfg["vpc_cidr"], st.get("vpc_id")],
        ["Internet Gateway ID", st.get("igw_id")],
        ["Public Subnet (CIDR: %s)" % cfg["public_subnet"]["cidr"], st.get("public_subnet_id")],
        ["Private Subnet (CIDR: %s)" % cfg["private_subnet"]["cidr"], st.get("private_subnet_id")],
        ["Public Route Table (0.0.0.0/0 → IGW)", st.get("public_route_table_id")],
        ["Private Route Table (0.0.0.0/0 → NAT)", st.get("private_route_table_id")],
        ["NAT Gateway ID", st.get("nat_gateway_id")],
        ["NAT Gateway Public Elastic IP", st.get("nat_public_ip")],
        ["NAT Gateway Private IP", st.get("nat_private_ip")],
        ["Public / Private NACL", f"{st.get('public_nacl_id', '—')} / {st.get('private_nacl_id', '—')}"],
        ["Bastion SG / Worker SG", f"{st.get('bastion_sg_id')} / {st.get('worker_sg_id')}"],
        ["SSH дозволено з", st.get("ssh_ingress_cidr")],
        ["Bastion Host Public IP", st.get("bastion_public_ip")],
        ["Bastion Host Private IP", st.get("bastion_private_ip")],
        ["OPC UA Worker Private IP", st.get("worker_private_ip")],
        ["OPC UA Worker Public IP", st.get("worker_public_ip") or "None (Secure Isolated)"],
        ["OPC UA endpoint (з бастіону)", f"opc.tcp://{st.get('worker_private_ip')}:{cfg['service']['port']}{cfg['service']['endpoint_path']}"],
    ]
    print(tabulate(rows, tablefmt="fancy_grid"))
    t = st.get("timings_s", {})
    print("\nТривалість етапів, с:", ", ".join(f"{k}={v}" for k, v in t.items()))
    c = st.get("hourly_cost_usd", {})
    if c:
        print(f"Орієнтовна вартість (AWS): {c['total_per_hour']} $/год "
              f"(NAT {c['nat_gateway']} + 2×t3.micro {c['instances']} + IPv4 {c['public_ipv4']}), "
              f"≈ {c['total_per_month_730h']} $/міс")


def stand(action: str, cfg: dict) -> None:
    compose = os.path.join(ROOT, cfg["stand"]["compose_file"])
    if action == "up":
        print("[INFO] Docker-стенд data plane: docker compose up -d --build (перший раз 2–4 хв)...", flush=True)
        subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "--build"], check=True)
    else:
        subprocess.run(["docker", "compose", "-f", compose, "down", "-v", "--remove-orphans"], check=False)


def run_diagnostics(builder: VPCNetworkBuilder, st: dict, cfg: dict, mode: str, skip_tests: bool) -> None:
    banner("МЕРЕЖЕВА ДІАГНОСТИКА")
    cidr = diag.cidr_analysis(cfg)
    print(diag.cidr_text(cidr))
    live = builder.describe_for_report(st["vpc_id"])
    lpm_rows = diag.lpm_analysis(live["route_tables"], st)
    print("\nLongest Prefix Match:")
    print(tabulate(lpm_rows, headers=["Таблиця", "Адреса", "Кандидати", "Обраний маршрут"], tablefmt="grid"))
    sg_rows, acl_rows = diag.security_audit(live["security_groups"], live["network_acls"], st)

    ssh_cfg = diag.write_ssh_config(st, cfg, mode)
    print(f"\n[INFO] Згенеровано {ssh_cfg}. Підключення: ssh -F {ssh_cfg} cps-worker")
    tests, latency = None, None
    if not skip_tests:
        tester = diag.DataPlaneTester(st, cfg, mode, ssh_cfg)
        tests = tester.run_all()
        latency = tester.latency
        st.setdefault("diagnostics", {}).update({"tests": tests, "latency": latency})
        print("\n" + tabulate([[t["id"], t["name"], t["status"]] for t in tests],
                              headers=["ID", "Тест", "Результат"], tablefmt="fancy_grid"))
    diag.write_routing_report(REPORT, cfg, st, cidr, lpm_rows, sg_rows, acl_rows, tests, latency)
    st["cidr_analysis"] = cidr
    save_state(st)
    print(f"[INFO] Звіт маршрутизації: {REPORT}; стан: {STATE}")


def main() -> None:
    ap = argparse.ArgumentParser(description="ЛБ2 · варіант 3 · VPC + Bastion + OPC UA")
    ap.add_argument("--localstack", action="store_true", help="LocalStack (control plane) + Docker-стенд (data plane)")
    ap.add_argument("--destroy", action="store_true", help="видалити інфраструктуру з output/network_state.json")
    ap.add_argument("--diagnose", action="store_true", help="лише діагностика наявної інфраструктури")
    ap.add_argument("--skip-tests", action="store_true", help="без тестів data plane")
    ap.add_argument("--no-nacl", action="store_true", help="не створювати власні Network ACL")
    ap.add_argument("--keep-on-error", action="store_true", help="не видаляти ресурси при помилці")
    args = ap.parse_args()
    os.chdir(ROOT)

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    mode = "stand" if args.localstack else "aws"
    builder = VPCNetworkBuilder(CONFIG, localstack=args.localstack)

    if args.destroy:
        banner("ВИДАЛЕННЯ ІНФРАСТРУКТУРИ VPC")
        if not os.path.exists(STATE):
            sys.exit("Файл стану output/network_state.json не знайдено — нічого видаляти.")
        builder.destroy(load_state(STATE))
        if args.localstack:
            stand("down", cfg)
        for p in (builder.key_path, builder.key_path + ".pub"):
            if os.path.exists(p):
                os.chmod(p, 0o600)
                os.remove(p)
        os.replace(STATE, STATE.replace(".json", f".destroyed-{int(time.time())}.json"))
        print("[SUCCESS] Інфраструктуру видалено.")
        return

    if args.diagnose:
        st = load_state(STATE)
        builder.state = st
        run_diagnostics(builder, st, cfg, mode, args.skip_tests)
        return

    banner("РОЗГОРТАННЯ ІЗОЛЬОВАНОЇ ХМАРНОЇ МЕРЕЖІ VPC ТА BASTION HOST")
    print(f"  Варіант 3 · VPC {cfg['vpc_cidr']} · OPC UA Industrial Server TCP {cfg['service']['port']}\n")
    try:
        st = builder.deploy(enable_nacl=not args.no_nacl and cfg.get("enable_nacl", True))
        save_state(st)
        print_summary(st, cfg)
        if args.localstack and not args.skip_tests:
            stand("up", cfg)
        run_diagnostics(builder, st, cfg, mode, args.skip_tests)
        print("\n[INFO] Інфраструктура працює. Після перевірок видаліть її:  "
              f"python3 deploy_network.py --destroy{' --localstack' if args.localstack else ''}")
    except KeyboardInterrupt:
        print("\n[WARN] Перервано користувачем.")
        save_state(builder.state)
        _cleanup(builder, args)
    except Exception as ex:  # noqa: BLE001
        print(f"\n[ERROR] {type(ex).__name__}: {ex}")
        save_state(builder.state)
        _cleanup(builder, args)
        sys.exit(1)


def _cleanup(builder: VPCNetworkBuilder, args) -> None:
    if args.keep_on_error:
        print("[INFO] --keep-on-error: ресурси залишено; видалення: python3 deploy_network.py --destroy")
        return
    print("[CLEANUP] Аварійне видалення створених ресурсів (NAT Gateway тарифікується)...")
    builder.destroy(builder.state)


if __name__ == "__main__":
    main()
