"""
Мережева діагностика ЛБ2 (варіант 3).

Статична частина (працює і з AWS, і з LocalStack):
    * розрахунок адресного простору: N_usable = 2^(32-M) - 5, закон збереження, перетини;
    * аналіз таблиць маршрутизації за алгоритмом Longest Prefix Match (LPM);
    * аудит правил Security Groups і Network ACL.

Динамічна частина (data plane, через SSH):
    * T1 прямий SSH до приватної IP має НЕ працювати;
    * T2 SSH ProxyJump через бастіон;
    * T3 вихідна IP приватного вузла = Elastic IP NAT Gateway;
    * T4 traceroute: перший вузол — NAT Gateway;
    * T5 порт OPC UA 4840 доступний з бастіону;
    * T6 заборонений порт (8080) з бастіону недоступний (ізоляція SG/NACL);
    * T7 читання змінних OPC UA через SSH-тунель (ProxyJump + port forwarding);
    * вимірювання затримок: ping, час встановлення SSH, TCP-connect напряму (IGW) і через NAT.

Режими: "aws" — справжня VPC; "stand" — Docker-стенд із тими самими адресами (для LocalStack).
"""

import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from tabulate import tabulate

FORBIDDEN_TEST_PORT = 8080
TUNNEL_LOCAL_PORT = 14840


# =============================================================== CIDR
def cidr_analysis(cfg: Dict[str, Any]) -> Dict[str, Any]:
    vpc = ipaddress.ip_network(cfg["vpc_cidr"])
    subnets = {"public": ipaddress.ip_network(cfg["public_subnet"]["cidr"]),
               "private": ipaddress.ip_network(cfg["private_subnet"]["cidr"])}
    rows = []
    for name, net in subnets.items():
        total = net.num_addresses
        rows.append({
            "subnet": name, "cidr": str(net), "prefix": net.prefixlen,
            "total": total, "usable": total - 5,
            "reserved": {
                "network": str(net.network_address),
                "vpc_router": str(net.network_address + 1),
                "amazon_dns": str(net.network_address + 2),
                "future_use": str(net.network_address + 3),
                "broadcast": str(net.broadcast_address),
            },
            "first_usable": str(net.network_address + 4),
            "last_usable": str(net.broadcast_address - 1),
            "inside_vpc": net.subnet_of(vpc),
        })
    used = sum(n.num_addresses for n in subnets.values())
    overlap = subnets["public"].overlaps(subnets["private"])
    return {
        "vpc": {"cidr": str(vpc), "prefix": vpc.prefixlen, "total": vpc.num_addresses,
                "possible_24_subnets": 2 ** (24 - vpc.prefixlen),
                "amazon_dns": str(vpc.network_address + 2)},
        "subnets": rows,
        "conservation": {"sum_subnets": used, "vpc_total": vpc.num_addresses,
                         "holds": used <= vpc.num_addresses,
                         "utilization_pct": round(100 * used / vpc.num_addresses, 3)},
        "overlap": overlap,
    }


def cidr_text(a: Dict[str, Any]) -> str:
    v = a["vpc"]
    lines = [f"VPC {v['cidr']}: M = {v['prefix']}, 2^(32-{v['prefix']}) = {v['total']} адрес, "
             f"можливих підмереж /24: {v['possible_24_subnets']}, Amazon DNS: {v['amazon_dns']}"]
    rows = []
    for s in a["subnets"]:
        r = s["reserved"]
        rows.append([s["subnet"], s["cidr"], f"2^(32-{s['prefix']}) = {s['total']}",
                     f"{s['total']} - 5 = {s['usable']}", f"{s['first_usable']} – {s['last_usable']}",
                     f"{r['network']}, {r['vpc_router']}, {r['amazon_dns']}, {r['future_use']}, {r['broadcast']}"])
    lines.append(tabulate(rows, headers=["Підмережа", "CIDR", "Усього", "N_usable", "Діапазон вузлів", "Зарезервовано"],
                          tablefmt="grid"))
    c = a["conservation"]
    lines.append(f"Закон збереження: Σ 2^(32-M_i) = {c['sum_subnets']} ≤ 2^(32-M_VPC) = {c['vpc_total']} → "
                 f"{'виконується' if c['holds'] else 'ПОРУШЕНО'} (використано {c['utilization_pct']} % простору VPC); "
                 f"перетин підмереж: {'ТАК' if a['overlap'] else 'немає'}")
    return "\n".join(lines)


# =============================================================== LPM
def _route_target(route: Dict[str, Any]) -> str:
    for key in ("NatGatewayId", "GatewayId", "TransitGatewayId", "VpcPeeringConnectionId",
                "NetworkInterfaceId", "InstanceId"):
        if route.get(key):
            return route[key]
    return "?"


def lpm_analysis(route_tables: List[Dict[str, Any]], state: Dict[str, Any]) -> List[List[str]]:
    """Для кожної таблиці маршрутів і контрольної адреси обирає маршрут з найдовшим префіксом."""
    names = {state.get("public_route_table_id"): "public-rt", state.get("private_route_table_id"): "private-rt"}
    destinations = [
        (state.get("worker_private_ip") or "10.30.50.20", "OPC UA worker"),
        (state.get("bastion_private_ip") or "10.30.2.10", "Bastion"),
        ("10.30.99.7", "адреса VPC без підмережі"),
        ("8.8.8.8", "Інтернет (Google DNS)"),
        ("104.16.0.1", "Інтернет (PyPI/CDN)"),
    ]
    rows = []
    for rt in route_tables:
        rt_name = names.get(rt["RouteTableId"])
        if not rt_name:
            continue
        routes = [(ipaddress.ip_network(r["DestinationCidrBlock"]), _route_target(r))
                  for r in rt.get("Routes", []) if r.get("DestinationCidrBlock")]
        for ip, label in destinations:
            addr = ipaddress.ip_address(ip)
            matches = [(net, tgt) for net, tgt in routes if addr in net]
            if not matches:
                rows.append([rt_name, f"{ip} ({label})", "—", "немає маршруту (blackhole)"])
                continue
            best = max(matches, key=lambda m: m[0].prefixlen)
            cand = ", ".join(f"{n}" for n, _ in sorted(matches, key=lambda m: -m[0].prefixlen))
            rows.append([rt_name, f"{ip} ({label})", cand, f"{best[0]} → {best[1]}"])
    return rows


# =============================================================== аудит
def security_audit(sgs: List[Dict[str, Any]], acls: List[Dict[str, Any]], state: Dict[str, Any]) -> Tuple[List, List]:
    sg_names = {g["GroupId"]: g["GroupName"] for g in sgs}
    sg_rows = []
    for g in sgs:
        if g["GroupId"] not in (state.get("bastion_sg_id"), state.get("worker_sg_id")):
            continue
        for p in g.get("IpPermissions", []):
            port = "all" if p.get("IpProtocol") == "-1" else (
                f"{p.get('FromPort')}" if p.get("FromPort") == p.get("ToPort") else f"{p.get('FromPort')}-{p.get('ToPort')}")
            sources = [r["CidrIp"] for r in p.get("IpRanges", [])] + \
                      [f"SG {sg_names.get(u['GroupId'], u['GroupId'])}" for u in p.get("UserIdGroupPairs", [])]
            sg_rows.append([g["GroupName"], "inbound", p.get("IpProtocol"), port, ", ".join(sources)])
    acl_rows = []
    proto = {"6": "tcp", "17": "udp", "1": "icmp", "-1": "all"}
    for a in acls:
        if a["NetworkAclId"] not in (state.get("public_nacl_id"), state.get("private_nacl_id")):
            continue
        name = "public-nacl" if a["NetworkAclId"] == state.get("public_nacl_id") else "private-nacl"
        for e in sorted(a.get("Entries", []), key=lambda x: (x["Egress"], x["RuleNumber"])):
            pr = e.get("PortRange")
            ports = f"{pr['From']}-{pr['To']}" if pr and pr["From"] != pr["To"] else (str(pr["From"]) if pr else "all")
            rule = "*" if e["RuleNumber"] == 32767 else e["RuleNumber"]
            acl_rows.append([name, "egress" if e["Egress"] else "ingress", rule, proto.get(str(e["Protocol"]), e["Protocol"]),
                             ports, e.get("CidrBlock"), e["RuleAction"]])
    return sg_rows, acl_rows


# =============================================================== SSH
def write_ssh_config(state: Dict[str, Any], cfg: Dict[str, Any], mode: str, out_dir: str = "output") -> str:
    """ssh_config з ProxyJump: `ssh -F output/ssh_config cps-worker`."""
    os.makedirs(out_dir, exist_ok=True)
    key = os.path.abspath(state["key_path"])
    user = cfg.get("ssh_user", "ubuntu")
    if mode == "stand":
        st = cfg["stand"]
        b_host, b_port = st["bastion_ssh_host"], st["bastion_ssh_port"]
        hostkeys = "  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n  LogLevel ERROR\n"
    else:
        b_host, b_port = state["bastion_public_ip"], 22
        hostkeys = (f"  StrictHostKeyChecking accept-new\n"
                    f"  UserKnownHostsFile {os.path.abspath(os.path.join(out_dir, 'known_hosts'))}\n")
    common = f"  User {user}\n  IdentityFile {key}\n  IdentitiesOnly yes\n  ConnectTimeout 8\n{hostkeys}"
    text = (f"# Згенеровано deploy_network.py (режим: {mode})\n"
            f"Host cps-bastion\n  HostName {b_host}\n  Port {b_port}\n{common}\n"
            f"Host cps-worker\n  HostName {state['worker_private_ip']}\n  Port 22\n  ProxyJump cps-bastion\n{common}")
    path = os.path.join(out_dir, "ssh_config")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)
    return path


class DataPlaneTester:
    def __init__(self, state: Dict[str, Any], cfg: Dict[str, Any], mode: str, ssh_config: str):
        self.state, self.cfg, self.mode, self.ssh_config = state, cfg, mode, ssh_config
        self.worker_ip = state["worker_private_ip"]
        self.port = int(cfg["service"]["port"])
        self.results: List[Dict[str, Any]] = []
        self.latency: Dict[str, Any] = {}

    # ---------- helpers
    def ssh(self, host: str, cmd: str, timeout: int = 40) -> Tuple[int, str, float]:
        t0 = time.time()
        try:
            p = subprocess.run(["ssh", "-F", self.ssh_config, "-o", "BatchMode=yes", host, cmd],
                               capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip(), time.time() - t0
        except subprocess.TimeoutExpired:
            return 124, "timeout", time.time() - t0

    def record(self, test_id: str, name: str, ok: Optional[bool], detail: str) -> None:
        status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        self.results.append({"id": test_id, "name": name, "status": status, "detail": detail})
        print(f"[{status}] {test_id} {name}: {detail}", flush=True)

    def wait_ready(self) -> bool:
        print("[INFO] Очікування готовності SSH на бастіоні та OPC UA на приватному вузлі "
              "(в AWS cloud-init встановлює пакети через NAT, 3–6 хв)...", flush=True)
        deadline = time.time() + (900 if self.mode == "aws" else 180)
        stage = "bastion"
        while time.time() < deadline:
            if stage == "bastion":
                rc, _, _ = self.ssh("cps-bastion", "true", timeout=20)
                if rc == 0:
                    print("[INFO] Бастіон приймає SSH.", flush=True)
                    stage = "worker"
                    continue
            else:
                rc, out, _ = self.ssh("cps-worker", f"nc -z -w 2 127.0.0.1 {self.port} && echo OPCUA_UP", timeout=30)
                if rc == 0 and "OPCUA_UP" in out:
                    print("[INFO] OPC UA сервер слухає порт.", flush=True)
                    return True
            time.sleep(10)
        return False

    def nat_expected_ip(self) -> Optional[str]:
        if self.mode == "aws":
            return self.state.get("nat_public_ip")
        # Стенд: вихідна IP контейнера nat-gateway (далі — NAT Docker Desktop і вашого роутера)
        compose = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               self.cfg["stand"]["compose_file"])
        try:
            p = subprocess.run(["docker", "compose", "-f", compose, "exec", "-T", "nat-gateway",
                                "curl", "-s", "--max-time", "10", "https://checkip.amazonaws.com"],
                               capture_output=True, text=True, timeout=30)
            return p.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    # ---------- tests
    def t1_direct_ssh_blocked(self) -> None:
        key = self.state["key_path"]
        t0 = time.time()
        try:
            p = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                                f"{self.cfg.get('ssh_user', 'ubuntu')}@{self.worker_ip}", "true"],
                               capture_output=True, text=True, timeout=15)
            rc, out = p.returncode, (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]
        except subprocess.TimeoutExpired:
            rc, out = 124, ["timeout"]
        self.record("T1", "Прямий SSH до приватної IP заблоковано", rc != 0,
                    f"ssh ubuntu@{self.worker_ip} → rc={rc}, {out[0][:80]} ({time.time() - t0:.1f} с)")

    def t2_proxyjump(self) -> None:
        rc, out, dt = self.ssh("cps-worker", "hostname; ip -4 -o addr show scope global | awk '{print $4}' | head -1")
        self.record("T2", "SSH ProxyJump через бастіон", rc == 0,
                    f"{' | '.join(out.splitlines()[:2])} ({dt:.2f} с)" if rc == 0 else out[-120:])

    def t3_nat_ip(self) -> None:
        rc, out, _ = self.ssh("cps-worker", "curl -s --max-time 10 https://checkip.amazonaws.com")
        got = out.strip().splitlines()[-1] if rc == 0 and out.strip() else None
        expected = self.nat_expected_ip()
        self.state.setdefault("diagnostics", {})["worker_egress_ip"] = got
        ok = bool(got) and got == expected
        self.record("T3", "Вихідна IP приватного вузла = IP NAT", ok,
                    f"worker бачить {got}, очікується {expected}")

    def t4_traceroute(self) -> None:
        nat_ip = self.state.get("nat_private_ip") if self.mode == "aws" else self.cfg["stand"]["nat_private_ip"]
        rc, out, _ = self.ssh("cps-worker", "traceroute -n -m 6 -w 2 -q 1 8.8.8.8", timeout=60)
        hops = [l.strip() for l in out.splitlines() if re.match(r"^\s*\d+\s", l)]
        self.state.setdefault("diagnostics", {})["traceroute"] = hops
        first = hops[0] if hops else "немає даних"
        found = bool(nat_ip) and any(nat_ip in h for h in hops[:3])
        self.record("T4", "traceroute: трафік іде через NAT Gateway", True if found else None,
                    f"перший вузол: {first}; NAT private IP {nat_ip} {'знайдено' if found else 'не видно (ICMP може фільтруватися)'}")

    def t5_service_port(self) -> None:
        rc, out, _ = self.ssh("cps-bastion", f"nc -z -v -w 3 {self.worker_ip} {self.port} 2>&1")
        self.record("T5", f"Порт OPC UA {self.port} доступний з бастіону", rc == 0, out.splitlines()[-1][:90] if out else "")

    def t6_forbidden_port(self) -> None:
        rc, out, _ = self.ssh("cps-bastion", f"nc -z -v -w 3 {self.worker_ip} {FORBIDDEN_TEST_PORT} 2>&1")
        self.record("T6", f"Порт {FORBIDDEN_TEST_PORT} закритий (ізоляція SG/NACL)", rc != 0,
                    out.splitlines()[-1][:90] if out else f"rc={rc}")

    def t7_opcua_read(self) -> None:
        try:
            from asyncua import Client  # noqa: F401
        except ImportError:
            self.record("T7", "Читання змінних OPC UA через SSH-тунель", None, "бібліотеку asyncua не встановлено локально")
            return
        tunnel = subprocess.Popen(["ssh", "-F", self.ssh_config, "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                                   "-N", "-L", f"127.0.0.1:{TUNNEL_LOCAL_PORT}:{self.worker_ip}:{self.port}", "cps-bastion"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(30):
                try:
                    socket.create_connection(("127.0.0.1", TUNNEL_LOCAL_PORT), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.5)
            values = asyncio.run(self._read_opcua())
            self.state.setdefault("diagnostics", {})["opcua_values"] = values
            self.record("T7", "Читання змінних OPC UA через SSH-тунель", True,
                        ", ".join(f"{k}={v}" for k, v in values.items()))
        except Exception as err:  # noqa: BLE001
            self.record("T7", "Читання змінних OPC UA через SSH-тунель", False, str(err)[:120])
        finally:
            tunnel.terminate()

    async def _read_opcua(self) -> Dict[str, Any]:
        from asyncua import Client
        svc = self.cfg["service"]
        url = f"opc.tcp://127.0.0.1:{TUNNEL_LOCAL_PORT}{svc['endpoint_path']}"
        async with Client(url=url, timeout=10) as client:
            ns = await client.get_namespace_index(svc["namespace_uri"])
            out = {}
            for name in ("Temperature_C", "Pressure_bar", "MotorSpeed_rpm", "Status", "Hostname"):
                node = await client.nodes.objects.get_child([f"{ns}:ProductionLine", f"{ns}:{name}"])
                out[name] = await node.read_value()
            return out

    # ---------- latency
    def measure_latency(self) -> None:
        lat: Dict[str, Any] = {}
        rc, out, _ = self.ssh("cps-bastion", f"ping -c 5 -i 0.3 -q {self.worker_ip}")
        m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", out)
        if m:
            lat["ping_bastion_to_worker_ms"] = {"min": float(m.group(1)), "avg": float(m.group(2)), "max": float(m.group(3))}
        for host in ("cps-bastion", "cps-worker"):
            times = [self.ssh(host, "true")[2] for _ in range(3)]
            lat[f"ssh_setup_{host}_s"] = round(sum(times) / len(times), 3)
        fmt = "%{time_connect} %{time_starttransfer}"
        for host, label in (("cps-bastion", "via_igw_public_ip"), ("cps-worker", "via_nat_gateway")):
            rc, out, _ = self.ssh(host, f"for i in 1 2 3; do curl -s -o /dev/null -w '{fmt}\\n' https://checkip.amazonaws.com; done")
            vals = [tuple(map(float, l.split())) for l in out.splitlines() if re.match(r"^[\d.]+ [\d.]+$", l.strip())]
            if vals:
                lat[f"https_{label}_ms"] = {"tcp_connect": round(1000 * sum(v[0] for v in vals) / len(vals), 1),
                                            "ttfb": round(1000 * sum(v[1] for v in vals) / len(vals), 1)}
        self.latency = lat
        print("[INFO] Затримки: " + json.dumps(lat, ensure_ascii=False), flush=True)

    def run_all(self) -> List[Dict[str, Any]]:
        if not self.wait_ready():
            self.record("T0", "Готовність вузлів", False, "бастіон або OPC UA не відповіли вчасно")
        self.t1_direct_ssh_blocked()
        self.t2_proxyjump()
        self.t3_nat_ip()
        self.t4_traceroute()
        self.t5_service_port()
        self.t6_forbidden_port()
        self.t7_opcua_read()
        self.measure_latency()
        return self.results


# =============================================================== звіт
def write_routing_report(path: str, cfg: Dict[str, Any], state: Dict[str, Any], cidr: Dict[str, Any],
                         lpm_rows: List, sg_rows: List, acl_rows: List,
                         tests: Optional[List[Dict[str, Any]]], latency: Optional[Dict[str, Any]]) -> str:
    parts = [
        "=" * 78,
        f" ЗВІТ МАРШРУТИЗАЦІЇ ТА БЕЗПЕКИ VPC · ЛБ2 · варіант {cfg.get('variant')} · бекенд: {state.get('backend')}",
        "=" * 78,
        "\n1. АДРЕСНИЙ ПРОСТІР\n" + cidr_text(cidr),
        "\n2. LONGEST PREFIX MATCH (вибір маршруту для контрольних адрес)\n" +
        tabulate(lpm_rows, headers=["Таблиця", "Адреса призначення", "Кандидати (за довжиною префікса)", "Обраний маршрут"],
                 tablefmt="grid"),
        "\n3. SECURITY GROUPS (stateful)\n" +
        tabulate(sg_rows, headers=["Група", "Напрям", "Протокол", "Порт", "Джерело"], tablefmt="grid"),
    ]
    if acl_rows:
        parts.append("\n4. NETWORK ACL (stateless, правила за зростанням номера, '*' = deny all)\n" +
                     tabulate(acl_rows, headers=["NACL", "Напрям", "Правило", "Протокол", "Порти", "CIDR", "Дія"],
                              tablefmt="grid"))
    if tests:
        parts.append("\n5. ТЕСТИ DATA PLANE\n" +
                     tabulate([[t["id"], t["name"], t["status"], t["detail"]] for t in tests],
                              headers=["ID", "Тест", "Результат", "Деталі"], tablefmt="grid", maxcolwidths=[None, 30, None, 60]))
    if latency:
        parts.append("\n6. ЗАТРИМКИ\n" + json.dumps(latency, ensure_ascii=False, indent=2))
    text = "\n".join(parts) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def docker_available() -> bool:
    return shutil.which("docker") is not None
