# ЛБ5 · Скріншоти для звіту (розділ 6, рисунки 6.1–6.8)

Команди — з теки `lab5_sqs_queues`. Для AWS приберіть `--localstack`.

| Рисунок | Команда | Що має бути на знімку |
|---|---|---|
| 6.1 | `bash scripts/localstack.sh status` (AWS: `aws sts get-caller-identity`) | готовність середовища |
| 6.2 | `bash run_mac.sh --localstack --keep` (6.2–6.6 — з цього виводу) | створення DLQ і основної черги (URL) |
| 6.3 | ↑ | `[PRODUCER SUCCESS] Успішно відправлено 1000 повідомлень за X с` + `[AUDIT] … буферизовано 1000/1000` |
| 6.4 | ↑ | рядки `[CONSUMER] Хвиля N` / `[CONSUMER FINISHED]` і паузи Redrive Policy |
| 6.5 | ↑ | «ПІДСУМКОВИЙ ЗВІТ БЕНЧМАРКІНГУ»: 950 коректних, 50 у DLQ (100 % poison), втрати 0.00 % |
| 6.6 | ↑ | таблиця розрахунків T_vis і T_drain |
| 6.7 | `bash scripts/cli_verify.sh --localstack` | атрибути черги (RedrivePolicy), `ApproximateNumberOfMessages` DLQ = 50, приклад повідомлення з DLQ |
| 6.8 | `bash run_mac.sh --localstack --destroy` | «Чергу видалено» ×2 |

Цифри для розрахунків у звіті — з `output/queue_benchmark_report.json` (поле `calculations`).
