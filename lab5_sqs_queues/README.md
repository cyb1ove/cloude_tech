# ЛБ 5 · Варіант 3 · SQS `cps-robot-telemetry` + Dead-Letter Queue

| Параметр | Значення |
|---|---|
| Черги | `cps-robot-telemetry-<hex>` (Standard, SSE-SQS) + `cps-robot-telemetry-dlq-<hex>` (утримання 14 д) |
| VisibilityTimeout / maxReceiveCount | 10 с / 2 |
| Повідомлення | 1000 (кутові енкодери 6 суглобів, 4 роботи), 5 % poison pill = 50 |
| Обробники | 8 потоків (ThreadPoolExecutor), long polling 2 с, DeleteMessageBatch |

## Запуск

```bash
bash run_mac.sh --keep                 # AWS; черги лишаються → bash scripts/cli_verify.sh → bash run_mac.sh --destroy
bash scripts/localstack.sh start
bash run_mac.sh --localstack --keep    # LocalStack
bash scripts/cli_verify.sh --localstack
bash run_mac.sh --localstack --destroy
bash run_mac.sh --localstack --redrive --keep   # + повернення повідомлень з DLQ (StartMessageMoveTask)
```

Сценарій: створення DLQ і основної черги з RedrivePolicy → 1000 повідомлень (SendMessageBatch) → простій обробників 5 с
(перевірка буферизації) → хвилі обробки 8 потоками (poison не видаляються) → після 2 отримань SQS переносить їх у DLQ →
аналіз DLQ (збіг sequence_id з poison) → розрахунок T_vis і T_drain. Звіти: `output/queue_benchmark_report.json`, `output/dlq_analysis.json`.
Тривалість: 1–2 хв (очікування VisibilityTimeout).
