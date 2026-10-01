# ЛБ3 · Скріншоти для звіту (розділ 6, рисунки 6.1–6.11)

Усі команди — з теки `lab3_object_storage`. Для AWS приберіть `--localstack`.

| Рисунок | Команда | Що має бути на знімку |
|---|---|---|
| 6.1 | `bash scripts/localstack.sh start` → `bash scripts/localstack.sh status` (AWS: `aws sts get-caller-identity`) | LocalStack готовий / ваш ARN |
| 6.2 | `bash run_mac.sh --localstack` (далі 6.2–6.8 — з цього ж виводу, прокручуючи) | таблиця «РОЗРАХУНОК ВАРТОСТІ ЗБЕРІГАННЯ» зі сценаріями A, B, C |
| 6.3 | ↑ | рядки від «Створення об'єктного бакета…» до «Бакет налаштовано: BPA, версійність…» |
| 6.4 | ↑ | 20 рядків `[UPLOAD] Пакет #N … [SUCCESS (HTTP 200)]` + «Успішно завантажено 20/20» (можна 2 знімки) |
| 6.5 | ↑ | блок «Тестування версійності»: нова версія, `DeleteMarker=True`, «об'єкт відновлено» |
| 6.6 | ↑ | таблиця «ПОТОЧНИЙ СТАН ОБ'ЄКТІВ У S3» |
| 6.7 | ↑ | «ІСТОРІЯ ВЕРСІЙ» + «Стан версій seq0002 … маркер видалення» |
| 6.8 | ↑ | таблиця конфігурації безпеки/lifecycle і таблиця тестів S1–S6 |
| 6.9 | `bash scripts/cli_verify.sh --localstack` | блоки «1. Шифрування» (AES256) і «2. Правила життєвого циклу» |
| 6.10 | ↑ (кінець виводу) | «5. Presigned GET + curl -I»: `HTTP/1.1 200`, `x-amz-server-side-encryption: AES256`, `x-amz-version-id` |
| 6.11 | `python3 run_lab3.py --destroy --localstack` (після всіх знімків!) | «Бакет … видалено» |

Цифри для звіту (якщо змінюєте параметри): `output/cost_model.json`, `output/s3_lifecycle_status.json`.
