# ЛБ 3 · Варіант 3 · S3-сховище телеметрії розумних лічильників

| Параметр | Значення |
|---|---|
| Префікс бакета | `cps-smart-meter-<8 hex>` |
| Датчик | розумний лічильник електроенергії `CPS-SMART-METER-03` |
| Пакети | 20 × ≈2 КБ JSON (трифазні вимірювання + профіль навантаження за 15 хв) |
| Life cycle | STANDARD → STANDARD_IA (60 д) → GLACIER (120 д) → видалення (730 д); неактуальні версії → GLACIER (30 д) → видалення (90 д) |
| Presigned URL TTL | 300 с |
| Захист | Block Public Access (4/4), BucketOwnerEnforced, SSE-S3 AES256, версійність, політика DenyInsecureTransport (лише AWS) |

## Запуск

```bash
# AWS
bash run_mac.sh                     # створити бакет, 20 PUT через presigned URL, версії, тести безпеки (бакет лишається)
bash scripts/cli_verify.sh          # перевірки з методички (encryption, lifecycle, curl -I presigned)
bash run_mac.sh --destroy           # видалити всі версії та бакет

# LocalStack
bash scripts/localstack.sh start
bash run_mac.sh --localstack
bash scripts/cli_verify.sh --localstack
bash run_mac.sh --localstack --destroy

python3 run_lab3.py --cost-only     # лише модель вартості (без AWS)
```

## Що перевіряється

* **Версійність:** перезапис `seq0001` створює нову версію; стара читається за `VersionId`.
  `seq0002` видаляється без `VersionId`, з'являється **DELETE MARKER** (об'єкт → 404), потім маркер видаляється і об'єкт відновлюється.
* **Безпека (S1–S6):** анонімний GET → 403; presigned GET → 200, SHA-256 збігається, `x-amz-server-side-encryption: AES256`;
  прострочений URL, змінений підпис, інший Content-Type, запит по HTTP → 403.
  LocalStack і moto не перевіряють підписи, тому там S1, S3–S5 позначаються WARN (в AWS очікується 403).
* **Вартість:** `output/cost_model.json` — модель на 24 місяці для 1000 лічильників (сценарії A/B/C).

## Файли результатів

`output/telemetry_manifest.json` (ключі, розміри, SHA-256, HTTP, VersionId), `output/s3_lifecycle_status.json`
(налаштування безпеки, lifecycle, тести), `output/cost_model.json`.
