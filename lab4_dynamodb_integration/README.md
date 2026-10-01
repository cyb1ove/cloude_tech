# ЛБ 4 · Варіант 3 · DynamoDB `cps-robot-arms`

| Параметр | Значення |
|---|---|
| Таблиця | `cps-robot-arms-<8 hex>`, PAY_PER_REQUEST, SSE, TTL `ExpiresAt` (30 д) |
| Ключ | PK `RobotID` (S), SK `Timestamp` (N) |
| GSI | `GSI_OperationalState_JointTorque`: PK `OperationalState` (S), SK `JointTorqueNm` (N), ALL |
| Дані | 10 роботів × 100 вимірювань (60 с): `JointTorqueNm`, `AngularVelocityRadS`, `MotorTempC`, IDLE 90 % / MOVING 5 % / ERROR 5 % |

## Запуск

```bash
bash run_mac.sh                     # AWS: створити, завантажити 1000, Conditional Writes, бенчмарк, видалити
bash run_mac.sh --keep              # залишити таблицю → bash scripts/cli_verify.sh → bash run_mac.sh --destroy
bash scripts/localstack.sh start && bash run_mac.sh --localstack --keep     # LocalStack
bash scripts/cli_verify.sh --localstack && bash run_mac.sh --localstack --destroy
```

Бенчмарк: 4 операції × (1 прогрів + 10 повторів) — Query PK (eventual/strong), Query GSI, Scan + Filter; медіана і p95 затримки,
ScannedCount, RCU. Результати: `output/benchmark_report.json`, `output/query_samples.json`, графік `output/benchmark_chart.png`.
