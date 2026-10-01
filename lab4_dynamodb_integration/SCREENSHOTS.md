# ЛБ4 · Скріншоти для звіту (розділ 6, рисунки 6.1–6.9)

Команди — з теки `lab4_dynamodb_integration`. Для AWS приберіть `--localstack`.

| Рисунок | Команда | Що має бути на знімку |
|---|---|---|
| 6.1 | `bash scripts/localstack.sh status` (AWS: `aws sts get-caller-identity`) | готовність середовища |
| 6.2 | `bash run_mac.sh --localstack --keep` (6.2–6.6 — з цього виводу) | «Таблиця … успішно створена та активна» + таблиця схеми (PK, SK, GSI, TTL) |
| 6.3 | ↑ | «Завантажено 1000 елементів за X с» і «Розподіл станів» |
| 6.4 | ↑ | блок «Conditional Writes»: версія 2, `[WARN] Конфлікт версій! Спрацювало оптимістичне блокування`, повтор → версія 3 |
| 6.5 | ↑ | таблиця бенчмарку (Count, Scanned, Latency, p95, RCU) + рядок «Scan прочитав у … разів більше» |
| 6.6 | ↑ | таблиця «РОЗРАХУНОК WCU / RCU» |
| 6.7 | відкрийте `output/benchmark_chart.png` | графік Scan vs Query (**вставляється як рисунок**, не скріншот) |
| 6.8 | `bash scripts/cli_verify.sh --localstack` | describe-table (ключі, GSI), Query/Scan `--select COUNT` з ConsumedCapacity |
| 6.9 | `bash run_mac.sh --localstack --destroy` | «Таблицю … успішно видалено» |

Цифри для таблиці 7.1 звіту — з `output/benchmark_report.json` (поле `benchmark`).
