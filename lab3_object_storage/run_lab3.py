"""
ЛБ3 · Варіант 3 · Масштабоване об'єктне сховище S3 для телеметрії розумних лічильників.

    python3 run_lab3.py                 # AWS: бакет + 20 пакетів через Presigned URL + тести (бакет залишається)
    python3 run_lab3.py --localstack    # те саме в LocalStack
    python3 run_lab3.py --cleanup       # виконати й одразу видалити бакет
    python3 run_lab3.py --destroy [--localstack]   # видалити бакет з output/s3_lifecycle_status.json
    python3 run_lab3.py --cost-only     # лише розрахунок вартості (без AWS)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

if sys.version_info < (3, 10):
    sys.exit("Потрібен Python 3.10+. На macOS: bash run_mac.sh")
try:
    import requests
    from tabulate import tabulate
except ImportError:
    sys.exit("Не знайдено залежностей: bash run_mac.sh (або pip install -r requirements.txt)")

from botocore.exceptions import ClientError

from scripts import cost_model
from scripts.s3_storage_manager import S3StorageManager, log
from scripts.sensor_emulator import SensorTelemetryEmulator

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join("config", "s3_config.json")
MANIFEST = os.path.join("output", "telemetry_manifest.json")
STATUS = os.path.join("output", "s3_lifecycle_status.json")
COST = os.path.join("output", "cost_model.json")


def banner(t: str) -> None:
    print("\n" + "=" * 65 + f"\n  {t}\n" + "=" * 65)


def short(s: str, n: int = 18) -> str:
    return s if len(s) <= n else s[:n] + "…"


def object_key(tpl: str, device: str, packet: dict) -> str:
    ts = datetime.fromisoformat(packet["timestamp"].replace("Z", "+00:00"))
    return tpl.format(year=ts.year, month=f"{ts.month:02d}", day=f"{ts.day:02d}", device=device, seq=packet["sequence_id"])


# ------------------------------------------------------------------ тести безпеки
def _purge(mgr: S3StorageManager, key: str) -> None:
    """Видаляє тестовий об'єкт, якщо емулятор прийняв запит, який AWS мав би відхилити."""
    resp = mgr.s3.list_object_versions(Bucket=mgr.bucket_name, Prefix=key)
    for v in resp.get("Versions", []) + resp.get("DeleteMarkers", []):
        mgr.s3.delete_object(Bucket=mgr.bucket_name, Key=v["Key"], VersionId=v["VersionId"])


def security_tests(mgr: S3StorageManager, key: str, expected_sha: str, emulator: SensorTelemetryEmulator) -> list:
    results = []
    ls = mgr.localstack

    def rec(tid, name, ok, detail, expect_denied=False):
        status = "PASS" if ok else ("WARN" if (ls and expect_denied) else "FAIL")
        if status == "WARN":
            detail += " — емулятор не перевіряє підписи/авторизацію (в AWS очікується 403)"
        results.append([tid, name, status, detail])
        print(f"[{status}] {tid} {name}: {detail}", flush=True)

    # S1: анонімний запит без підпису
    if ls:
        public_url = f"{mgr.s3.meta.endpoint_url}/{mgr.bucket_name}/{key}"
    else:
        public_url = f"https://{mgr.bucket_name}.s3.{mgr.region}.amazonaws.com/{key}"
    r = requests.get(public_url, timeout=15)
    rec("S1", "Анонімний GET без підпису", r.status_code == 403, f"HTTP {r.status_code}", expect_denied=True)

    # S2: presigned GET — завантаження, цілісність, заголовки шифрування й версії
    url = mgr.generate_presigned_url(key, "get_object")
    r = requests.get(url, timeout=15)
    sha = emulator.sha256(r.content)
    enc = r.headers.get("x-amz-server-side-encryption")
    ver = r.headers.get("x-amz-version-id")
    rec("S2", "Presigned GET + SHA-256 + SSE", r.status_code == 200 and sha == expected_sha and bool(enc),
        f"HTTP {r.status_code}, sha256 {'збігається' if sha == expected_sha else 'НЕ збігається'}, "
        f"x-amz-server-side-encryption={enc}, x-amz-version-id={short(ver or '—', 12)}")

    # S3: прострочений presigned URL
    exp_url = mgr.generate_presigned_url(key.replace(".json", "_expired.json"), "put_object", expires_in=1,
                                         content_type="application/json")
    time.sleep(2.5)
    ok, code, _, _ = emulator.upload_via_presigned_url(exp_url, {"test": "expired"})
    rec("S3", "Прострочений URL (TTL = 1 с)", code == 403, f"HTTP {code}", expect_denied=True)
    if code == 200:
        _purge(mgr, key.replace(".json", "_expired.json"))

    # S4: підроблений підпис
    parts = urlsplit(mgr.generate_presigned_url(key, "get_object"))
    q = dict(parse_qsl(parts.query))
    sig = q.get("X-Amz-Signature", "")
    q["X-Amz-Signature"] = ("0" if sig[-1:] != "0" else "1") + sig[1:]
    r = requests.get(urlunsplit(parts._replace(query=urlencode(q))), timeout=15)
    rec("S4", "Змінений X-Amz-Signature", r.status_code == 403, f"HTTP {r.status_code}", expect_denied=True)

    # S5: інший Content-Type, ніж підписано
    url = mgr.generate_presigned_url(key.replace(".json", "_ctype.json"), "put_object", content_type="application/json")
    r = requests.put(url, data=b"{}", headers={"Content-Type": "text/plain"}, timeout=15)
    rec("S5", "PUT з непідписаним Content-Type", r.status_code == 403, f"HTTP {r.status_code}", expect_denied=True)
    if r.status_code == 200:
        _purge(mgr, key.replace(".json", "_ctype.json"))

    # S6: доступ по HTTP (без TLS) — блокує політика DenyInsecureTransport
    if ls:
        results.append(["S6", "Presigned GET по HTTP (без TLS)", "SKIP", "LocalStack працює лише по HTTP"])
        print("[SKIP] S6 Presigned GET по HTTP: LocalStack працює лише по HTTP", flush=True)
    else:
        http_url = mgr.generate_presigned_url(key, "get_object").replace("https://", "http://", 1)
        r = requests.get(http_url, timeout=15)
        rec("S6", "Presigned GET по HTTP (без TLS)", r.status_code == 403, f"HTTP {r.status_code} (політика aws:SecureTransport)")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="ЛБ3 · варіант 3 · S3 cps-smart-meter")
    ap.add_argument("--localstack", action="store_true")
    ap.add_argument("--cleanup", action="store_true", help="видалити бакет після виконання")
    ap.add_argument("--destroy", action="store_true", help="видалити бакет з output/s3_lifecycle_status.json")
    ap.add_argument("--cost-only", action="store_true", help="лише модель вартості")
    args = ap.parse_args()
    os.chdir(ROOT)
    os.makedirs("output", exist_ok=True)
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

    if args.destroy:
        with open(STATUS, encoding="utf-8") as f:
            st = json.load(f)
        S3StorageManager(CONFIG, args.localstack, bucket_name=st["bucket"]).delete_bucket()
        return

    banner("РОЗРАХУНОК ВАРТОСТІ ЗБЕРІГАННЯ (модель на 24 місяці)")
    cost = cost_model.simulate(cfg)
    with open(COST, "w", encoding="utf-8") as f:
        json.dump(cost, f, indent=2, ensure_ascii=False)
    a, s = cost["assumptions"], cost["summary"]
    print(f"Парк: {a['devices']} лічильників × 1 пакет/{a['interval_minutes']} хв × {a['packet_kb']} КБ "
          f"= {a['packets_per_month']:,} PUT/міс".replace(",", " "))
    print(tabulate([
        ["A: окремі об'єкти по 2 КБ (перехід < 128 КБ не виконується)", s["A_year1"], s["A_year2"],
         s["A_month24"]["volume_gb"]["STANDARD"], 0, 0],
        ["B: агрегація в хмарі ≈192 КБ/доба + lifecycle", s["B_year1"], s["B_year2"],
         s["B_month24"]["volume_gb"]["STANDARD"], s["B_month24"]["volume_gb"]["STANDARD_IA"], s["B_month24"]["volume_gb"]["GLACIER"]],
        ["C: пакетування на шлюзі (1 PUT/доба) + lifecycle", s["C_year1"], s["C_year2"],
         s["C_month24"]["volume_gb"]["STANDARD"], s["C_month24"]["volume_gb"]["STANDARD_IA"], s["C_month24"]["volume_gb"]["GLACIER"]],
    ], headers=["Сценарій", "Рік 1, $", "Рік 2, $", "STD, ГБ (міс 24)", "IA, ГБ", "GLACIER, ГБ"], tablefmt="fancy_grid"))
    m24 = s["A_month24"]
    print(f"Сценарій A, місяць 24: сховище {m24['storage']} $ + PUT {m24['put']} $ — вартість визначають запити, а не обсяг.")
    if args.cost_only:
        return

    banner("ПРОГРАМНЕ КЕРУВАННЯ МАСШТАБОВАНИМ СХОВИЩЕМ S3 ТА ТЕЛЕМЕТРІЄЮ")
    if args.localstack:
        log("INFO", "Режим LocalStack: S3 API -> http://localhost:4566")
    mgr = S3StorageManager(CONFIG, localstack=args.localstack)
    state = {"bucket": mgr.bucket_name, "region": mgr.region, "backend": "LocalStack" if args.localstack else "AWS",
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        mgr.create_secure_bucket()
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        # ---------------------------------------------------- телеметрія
        sc = cfg["sensor"]
        emu = SensorTelemetryEmulator(sc["device_id"], sc["sensor_type"], sc["packet_size_bytes"],
                                      cfg["cost_model"]["interval_minutes"])
        print(f"\n--- Генерація та завантаження {sc['packet_count']} пакетів телеметрії через Presigned URL "
              f"(TTL {mgr.ttl} с) ---")
        manifest = []
        packets = emu.generate_telemetry_batch(sc["packet_count"])
        for p in packets:
            key = object_key(sc["key_template"], sc["device_id"], p)
            url = mgr.generate_presigned_url(key, "put_object", content_type="application/json")
            ok, code, headers, dt = emu.upload_via_presigned_url(url, p)
            body = emu.encode(p)
            manifest.append({"seq": p["sequence_id"], "key": key, "bytes": len(body), "sha256": emu.sha256(body),
                             "http_status": code, "version_id": headers.get("x-amz-version-id"),
                             "sse": headers.get("x-amz-server-side-encryption"), "upload_ms": round(dt * 1000, 1),
                             "status": p["system_status"], "timestamp": p["timestamp"]})
            print(f"[UPLOAD] Пакет #{p['sequence_id']:>2} -> s3://{mgr.bucket_name}/{key} "
                  f"[{'SUCCESS' if ok else 'FAILED'} (HTTP {code}), {len(body)} Б, {dt * 1000:.0f} мс]", flush=True)
        ok_count = sum(1 for m in manifest if m["http_status"] == 200)
        log("SUCCESS" if ok_count == len(manifest) else "WARN", f"Успішно завантажено {ok_count}/{len(manifest)} пакетів.")

        # ---------------------------------------------------- версійність
        print("\n--- Тестування версійності об'єктів (Object Versioning) ---")
        k1 = manifest[0]["key"]
        corrected = dict(packets[0], system_status="CORRECTED", correction_reason="meter recalibration")
        log("INFO", f"Модифікація та перезапис існуючого об'єкта: {k1}")
        ok, code, headers, _ = emu.upload_via_presigned_url(
            mgr.generate_presigned_url(k1, "put_object", content_type="application/json"), corrected)
        log("SUCCESS" if ok else "ERROR", f"Нова версія {headers.get('x-amz-version-id')} (HTTP {code}); "
                                          f"попередня: {manifest[0]['version_id']}")
        old = mgr.s3.get_object(Bucket=mgr.bucket_name, Key=k1, VersionId=manifest[0]["version_id"])["Body"].read()
        log("INFO", f"Попередня версія доступна за VersionId, SHA-256 збігається з оригіналом: "
                    f"{emu.sha256(old) == manifest[0]['sha256']}")

        k2 = manifest[1]["key"]
        log("INFO", f"Видалення об'єкта без VersionId (створюється маркер видалення): {k2}")
        dm = mgr.s3.delete_object(Bucket=mgr.bucket_name, Key=k2)
        try:
            mgr.head(k2)
            visible = True
        except ClientError:
            visible = False
        log("INFO", f"DeleteMarker={dm.get('DeleteMarker')}, VersionId маркера={dm.get('VersionId')}; "
                    f"об'єкт {'видимий' if visible else 'прихований (404)'}")
        versions_with_marker = mgr.list_object_versions()
        mgr.s3.delete_object(Bucket=mgr.bucket_name, Key=k2, VersionId=dm["VersionId"])
        restored = mgr.head(k2)
        log("SUCCESS", f"Маркер видалено → об'єкт відновлено (VersionId {restored.get('VersionId')}).")

        # ---------------------------------------------------- безпека
        banner("ПЕРЕВІРКА БЕЗПЕКИ ДОСТУПУ")
        sec = security_tests(mgr, manifest[2]["key"], manifest[2]["sha256"], emu)
        posture = mgr.security_posture()

        # ---------------------------------------------------- звіти
        objects = mgr.list_bucket_objects()
        banner("ПОТОЧНИЙ СТАН ОБ'ЄКТІВ У S3")
        print(tabulate([[o["Key"].split("/")[-1], o["Size"], o["StorageClass"], short(o["ETag"], 14)] for o in objects],
                       headers=["Ключ об'єкта (…/day=DD/)", "Розмір (Б)", "Клас сховища", "ETag"], tablefmt="fancy_grid"))
        versions = mgr.list_object_versions()
        banner("ІСТОРІЯ ВЕРСІЙ (VERSIONING AUDIT)")
        print(tabulate([[v["Key"].split("/")[-1], short(v["VersionId"], 20), v["IsLatest"], v["Size"], v["Type"]]
                        for v in versions if v["Key"] in (k1, k2)] +
                       [["…", f"+ {len(versions) - sum(1 for v in versions if v['Key'] in (k1, k2))} інших версій", "", "", ""]],
                       headers=["Ключ об'єкта", "ID версії", "Latest", "Розмір", "Тип"], tablefmt="grid"))
        print("\nСтан версій seq0002 у момент, коли існував маркер видалення:")
        print(tabulate([[v["Key"].split("/")[-1], short(v["VersionId"], 20), v["IsLatest"], v["Type"]]
                        for v in versions_with_marker if v["Key"] == k2],
                       headers=["Ключ", "ID версії", "Latest", "Тип"], tablefmt="grid"))
        banner("КОНФІГУРАЦІЯ БЕЗПЕКИ ТА ЖИТТЄВОГО ЦИКЛУ")
        lc = posture["lifecycle"][0]
        print(tabulate([
            ["Block Public Access", ", ".join(f"{k}={v}" for k, v in posture["public_access_block"].items())
             if isinstance(posture["public_access_block"], dict) else posture["public_access_block"]],
            ["Object Ownership", posture["ownership"]],
            ["Versioning", posture["versioning"]],
            ["Шифрування за замовчуванням", posture["encryption"]],
            ["Політика бакета", ", ".join(posture["policy_statements"]) or "—"],
            ["Lifecycle", f"{lc['ID']} ({lc['Status']}), фільтр {lc.get('Filter')}"],
            ["  переходи", ", ".join(f"{t['StorageClass']}@{t['Days']}д" for t in lc.get("Transitions", []))],
            ["  видалення", f"{lc.get('Expiration', {}).get('Days')} д; неактуальні версії: "
                            f"GLACIER@{lc['NoncurrentVersionTransitions'][0]['NoncurrentDays']}д, "
                            f"видалення @{lc['NoncurrentVersionExpiration']['NoncurrentDays']}д"],
        ], tablefmt="fancy_grid"))
        print(tabulate(sec, headers=["ID", "Тест безпеки", "Результат", "Деталі"], tablefmt="grid",
                       maxcolwidths=[None, 28, None, 60]))

        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump({"bucket": mgr.bucket_name, "device_id": sc["device_id"], "packets": manifest,
                       "overwrite_test": {"key": k1, "new_version": headers.get("x-amz-version-id")},
                       "delete_marker_test": {"key": k2, "marker_version": dm.get("VersionId"),
                                              "restored_version": restored.get("VersionId")}},
                      f, indent=2, ensure_ascii=False)
        state.update({"posture": posture, "security_tests": sec, "object_count": len(objects),
                      "version_count": len(versions),
                      "avg_upload_ms": round(sum(m["upload_ms"] for m in manifest) / len(manifest), 1),
                      "cost_summary": cost["summary"]})
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
        log("INFO", f"Звіти збережено: {MANIFEST}, {STATUS}, {COST}")

        if args.cleanup:
            mgr.delete_bucket()
        else:
            log("INFO", f"Бакет залишено для перевірок. Видалення: python3 run_lab3.py --destroy"
                        f"{' --localstack' if args.localstack else ''}")
        log("SUCCESS", "Лабораторну роботу успішно виконано.")
    except Exception as ex:  # noqa: BLE001
        log("ERROR", f"{type(ex).__name__}: {ex}")
        log("INFO", f"Бакет {mgr.bucket_name} можна видалити: python3 run_lab3.py --destroy")
        sys.exit(1)


if __name__ == "__main__":
    main()
