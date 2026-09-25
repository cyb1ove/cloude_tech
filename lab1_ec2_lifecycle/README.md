# ЛБ 1 · Варіант 3 · `cps-sensor-aggregator`

| Параметр | Значення |
|---|---|
| Вузол КФС | `cps-sensor-aggregator` |
| Тип інстансу | `t3.micro` (2 vCPU, 1 GiB RAM) |
| AMI | Debian 12 (bookworm), amd64, шукається автоматично (власник `136693071363`) |
| EBS | 15 ГБ, gp3, зашифрований, `DeleteOnTermination=true` |
| User Data | Mosquitto MQTT Broker, listener `0.0.0.0:1883`, автентифікація за паролем |
| Security Group | 22/tcp ← лише ваш IP (/32), 1883/tcp ← 0.0.0.0/0 |

## Структура

```
lab1_ec2_lifecycle/
├── config/settings.json        # параметри варіанта 3 + тарифи для розрахунку вартості
├── scripts/
│   ├── __init__.py
│   ├── ec2_manager.py          # клас EC2LifecycleManager (Boto3)
│   ├── user_data.sh            # cloud-init: встановлення й налаштування Mosquitto
│   ├── cli_verify.sh           # незалежна перевірка ресурсів через AWS CLI (--localstack)
│   └── localstack.sh           # start/status/logs/stop емулятора LocalStack
├── output/                     # створюється автоматично
│   ├── instance_manifest.json
│   ├── lifecycle_report.json   # таймінги T_wait та розрахунок C_total
│   └── mqtt_credentials.json   # логін/пароль MQTT (не комітити!)
├── requirements.txt
├── run_mac.sh                  # запуск на macOS: python/awscli/venv + main.py
└── main.py
```

## Запуск на macOS (Intel / Apple Silicon)

1. Встановіть Homebrew, якщо його ще немає (https://brew.sh):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
2. Розпакуйте архів, відкрийте **Terminal** і перейдіть у теку проєкту:
   ```bash
   cd ~/Downloads/lab1_ec2_lifecycle
   ```
3. Задайте креденшали AWS:
   * власний акаунт: `aws configure` (AWS CLI встановиться на кроці 4, тож цей пункт можна виконати після нього);
   * AWS Academy: *AWS Details → AWS CLI → Show* і вставте блок у `~/.aws/credentials`
     (`open -e ~/.aws/credentials`), разом з `aws_session_token`. У `settings.json` поставте `"region_name": "us-east-1"`.
4. Запустіть:
   ```bash
   bash run_mac.sh            # повний цикл
   bash run_mac.sh --keep     # залишити інстанс для скріншотів / SSH
   bash run_mac.sh --purge    # після termination видалити також SG і пару ключів
   ```
   `run_mac.sh` сам знайде або встановить Python 3.10+ (`brew install python@3.12`) та AWS CLI
   (`brew install awscli`), створить `venv`, встановить залежності, перевірить креденшали й запустить `main.py`.

Повторні запуски можна робити й вручну: `source venv/bin/activate && python main.py`.
Повний цикл триває приблизно 5–8 хвилин.

### Перевірка брокера з Mac

```bash
brew install mosquitto jq
IP=$(jq -r .PublicIpAddress output/instance_manifest.json)
U=$(jq -r .username output/mqtt_credentials.json); P=$(jq -r .password output/mqtt_credentials.json)
mosquitto_sub -h "$IP" -p 1883 -u "$U" -P "$P" -t 'cps/#' -v -C 1     # отримає retained-статус ONLINE
ssh -i cps-lab1-v3-keypair.pem admin@"$IP"                             # лише з --keep
bash scripts/cli_verify.sh                                             # перевірка ресурсів через AWS CLI
```

### Типові проблеми на macOS

| Симптом | Причина / рішення |
|---|---|
| `Потрібен Python 3.10+, зараз 3.9` | Системний `/usr/bin/python3` від Xcode має версію 3.9. Запускайте через `bash run_mac.sh` |
| `xcrun: error: invalid active developer path` | `xcode-select --install` |
| `CERTIFICATE_VERIFY_FAILED` (Python з python.org) | Запустіть `/Applications/Python 3.x/Install Certificates.command` |
| `WARNING: UNPROTECTED PRIVATE KEY FILE` при ssh | `chmod 400 cps-lab1-v3-keypair.pem` |
| `ExpiredToken` | Сесія AWS Academy (4 год) закінчилась: оновіть креденшали в `~/.aws/credentials` |
| SSH «висне» | Змінилась ваша IP. SG дозволяє 22 лише з IP на момент створення: видаліть SG (`--purge`) і запустіть знову |

## Режим без акаунта AWS: LocalStack

LocalStack емулює API AWS на вашому комп'ютері. EC2 працює через Docker: кожен «інстанс» є контейнером
з образу `debian:12`, який зареєстровано як AMI `ami-000003`. Код керування той самий, змінюється лише
endpoint (`http://localhost:4566`) і ключі (`test`/`test`).

**Що потрібно:**
1. **Docker Desktop:** `brew install --cask docker`, відкрийте застосунок і дочекайтесь *Engine running*.
   У *Settings → Advanced* має бути ввімкнено *Allow the default Docker socket to be used*.
2. **Безкоштовний Auth Token LocalStack.** З березня 2026 LocalStack працює лише з обліковим записом.
   Зареєструйтесь на https://app.localstack.cloud (для некомерційного використання є безкоштовний план,
   для студентів також Student Plan через GitHub Student Developer Pack) і скопіюйте токен
   з розділу *Auth Tokens*.

**Запуск:**
```bash
bash scripts/localstack.sh start              # перший раз попросить токен і збереже його в .localstack_token
bash run_mac.sh --localstack --keep           # сценарій лабораторної проти емулятора
bash scripts/cli_verify.sh --localstack       # перевірка ресурсів через AWS CLI
bash scripts/localstack.sh status             # стан LocalStack і контейнерів-«інстансів»
bash scripts/localstack.sh stop               # зупинити все
```

**Перевірка брокера:** на macOS мережа контейнерів з хоста не видна, тому LocalStack пробрасує
порти з групи `default` на випадкові порти `127.0.0.1`. Програма знаходить їх сама
(`docker port`) і показує в таблиці (`MqttEndpoint`, `SshCommand`), наприклад:
```bash
mosquitto_sub -h 127.0.0.1 -p <порт з MqttEndpoint> -u "$U" -P "$P" -t 'cps/#' -v -C 1
docker ps --filter ancestor=localstack-ec2/debian-12-cps:ami-000003     # контейнер-«інстанс»
docker exec -it <ID> tail -n 30 /var/log/cps_user_data.log            # журнал User Data
```

**Чим LocalStack відрізняється від AWS (варто згадати у звіті):**
* «інстанс» є контейнером, а не ВМ: немає гіпервізора, systemd і справжнього тому EBS 15 ГБ.
  Параметри тому зберігаються лише як метадані API;
* Stop/Start ставить контейнер на паузу і знімає з паузи, тож таймінги очікування значно менші, ніж в AWS;
* перевірки `instance_status_ok` немає, тому програма їх пропускає;
* враховується лише Security Group `default`, тому програма відкриває порти 22/1883 саме в ній;
* вартість у `lifecycle_report.json` умовна: її пораховано за тарифами AWS для порівняння.

| Симптом | Рішення |
|---|---|
| `LocalStack не запущено` | `bash scripts/localstack.sh start` |
| `Docker не запущено` | Відкрийте Docker Desktop |
| LocalStack не стає готовим, у журналі помилка ліцензії | Перевірте токен: видаліть `.localstack_token` і запустіть `start` знову |
| `AMI ami-000003 не зареєстровано` | `bash scripts/localstack.sh stop && bash scripts/localstack.sh start` |
| Тест MQTT: «ще не готовий» довго | Контейнер встановлює Mosquitto через apt (потрібен інтернет). Дивіться `docker exec … tail /var/log/cps_user_data.log` |

## Важливо

* **Регіон.** В AWS Academy Learner Lab зазвичай доступні лише `us-east-1` / `us-west-2`.
  Змініть `region_name` у `settings.json`. AMI Debian 12 підбирається автоматично в будь-якому регіоні.
* **Кореневий пристрій.** У Debian це `/dev/xvda`, а не `/dev/sda1`, як в еталонному прикладі для Ubuntu.
  Код бере ім'я з AMI. Інакше EC2 створив би окремий додатковий диск, а корінь лишився б 8 ГБ.
* **Mosquitto 2.x** за замовчуванням слухає лише localhost, тому listener на `0.0.0.0` задано явно,
  а анонімний доступ вимкнено.
* **Тарифи** в `settings.json` (t3.micro $0.012/год, gp3 $0.0952/ГБ·міс, Frankfurt) перед здачею звіту
  звірте з актуальним прайсом AWS.
* Якщо скрипт впав, він сам намагається знищити інстанс. Перевірте в консолі, що нічого не лишилося.
