# ЛБ 2 · Варіант 3 · VPC + Bastion Host + OPC UA Industrial Server

| Параметр | Значення |
|---|---|
| VPC | `10.30.0.0/16` (DNS support + hostnames) |
| Публічна підмережа | `10.30.2.0/24`, eu-central-1a: Bastion `10.30.2.10`, NAT Gateway + Elastic IP |
| Приватна підмережа | `10.30.50.0/24`, eu-central-1a: OPC UA Industrial Server `10.30.50.20`, без публічної IP |
| Сервіс КФС | OPC UA (asyncua), `opc.tcp://10.30.50.20:4840/cps/opcua/` |
| Маршрути | public: `0.0.0.0/0 → IGW`; private: `0.0.0.0/0 → NAT Gateway` |
| Security Groups | bastion: 22 ← ваш IP/32; worker: 22, 4840, ICMP ← лише bastion SG |
| Network ACL | окремі для кожної підмережі (stateless, ефемерні порти 1024–65535) |

## Структура

```
lab2_vpc_network/
├── config/network_schema.json      # параметри варіанта 3, тарифи, LocalStack/стенд
├── scripts/
│   ├── vpc_builder.py              # клас VPCNetworkBuilder: VPC, IGW, NAT, RT, NACL, SG, EC2, destroy
│   ├── network_diagnostics.py      # CIDR, Longest Prefix Match, аудит SG/NACL, тести T1–T7, затримки
│   ├── opcua_server.py             # OPC UA сервер (той самий код для AWS і для стенду)
│   ├── worker_user_data.sh         # cloud-init приватного вузла (встановлення через NAT)
│   ├── bastion_user_data.sh        # cloud-init бастіону (посилення sshd)
│   ├── cli_verify.sh               # перевірка через AWS CLI (--localstack)
│   ├── localstack.sh               # LocalStack для ЛБ2 (EC2 mock)
│   └── stand.sh                    # Docker-стенд data plane
├── dataplane/                      # docker compose: bastion / nat-gateway / worker (ті самі IP)
├── output/                         # network_state.json, routing_report.txt, ssh_config
├── deploy_network.py               # головний сценарій
├── run_mac.sh                      # запуск на macOS
└── requirements.txt
```

## Варіант А: AWS

```bash
bash run_mac.sh                  # розгортання (≈5 хв) + діагностика (ще 3–6 хв, поки cloud-init ставить OPC UA через NAT)
bash scripts/cli_verify.sh       # незалежна перевірка ресурсів
ssh -F output/ssh_config cps-worker          # вхід на приватний вузол через ProxyJump
bash run_mac.sh --destroy        # ОБОВ'ЯЗКОВО після перевірок: NAT Gateway ≈ 0.052 $/год
```

Тести вручну (як у методичці):
```bash
KEY=cps-lab2-key.pem; B=$(jq -r .bastion_public_ip output/network_state.json); W=$(jq -r .worker_private_ip output/network_state.json)
ssh -i $KEY ubuntu@$W -o ConnectTimeout=5                             # 1: має бути timeout
ssh -i $KEY -o ProxyCommand="ssh -i $KEY -W %h:%p ubuntu@$B" ubuntu@$W  # 2: ProxyJump (або ssh -F output/ssh_config cps-worker)
ssh -F output/ssh_config cps-worker curl -s https://checkip.amazonaws.com   # 3: = NAT Elastic IP
ssh -F output/ssh_config cps-worker traceroute -n -m 5 8.8.8.8            # 4: перший вузол — NAT
```

## Варіант Б: без акаунта AWS — LocalStack + Docker-стенд

LocalStack емулює **API** (VPC, підмережі, NAT, маршрути, SG, NACL, EC2 mock), але пакети там не ходять.
Тому мережеві тести виконуються на Docker-стенді з **тими самими адресами**:

| Контейнер | Адреси | Роль |
|---|---|---|
| `bastion` | 10.30.2.10, SSH на `127.0.0.1:2222` | Bastion Host (Ubuntu 22.04, лише SSH) |
| `nat-gateway` | 10.30.2.254 / 10.30.50.254 | VPC router + NAT (MASQUERADE) + фільтр, що відтворює Worker SG |
| `worker` | 10.30.50.20 (не опубліковано) | OPC UA сервер; маршрут за замовчуванням → nat-gateway |

```bash
bash scripts/localstack.sh start           # Docker Desktop + токен LocalStack (як у ЛБ1)
bash run_mac.sh --localstack               # API у LocalStack + стенд + тести T1–T7
bash scripts/cli_verify.sh --localstack
bash scripts/stand.sh status               # контейнери та правила iptables «NAT/SG»
ssh -F output/ssh_config cps-worker        # ProxyJump через бастіон стенду
bash run_mac.sh --localstack --destroy     # видалити ресурси LocalStack і стенд
```

На стенді вихідна IP приватного вузла дорівнює IP контейнера `nat-gateway` (далі пакет ще раз транслює
Docker Desktop і ваш роутер). Тому доказом NAT є тест T3 (збіг IP) разом із T4 (перший вузол traceroute `10.30.50.254`).

> Стенд розрахований на **Docker Desktop (macOS/Windows)**: там мережі контейнерів недосяжні з хоста, тож T1 (прямий SSH) коректно завершується timeout. На Linux із нативним Docker хост бачить мережі контейнерів напряму, і T1 покаже FAIL.

## Тести діагностики

| ID | Перевірка | Очікування |
|---|---|---|
| T1 | прямий SSH до 10.30.50.20 | timeout (приватна адреса недосяжна) |
| T2 | SSH ProxyJump через бастіон | shell на `ip-10-30-50-20` |
| T3 | `curl checkip` з приватного вузла | = Elastic IP NAT Gateway |
| T4 | `traceroute 8.8.8.8` | через NAT Gateway |
| T5 | `nc -z 10.30.50.20 4840` з бастіону | порт OPC UA відкритий |
| T6 | `nc -z 10.30.50.20 8080` з бастіону | закрито (SG/NACL) |
| T7 | SSH-тунель + клієнт asyncua | значення Temperature_C, Pressure_bar, … |

Результати, розрахунки CIDR, LPM-аналіз і правила SG/NACL зберігаються в `output/routing_report.txt`.

## Типові проблеми

| Симптом | Рішення |
|---|---|
| T0/T2 FAIL в AWS одразу після розгортання | cloud-init ще працює: `python3 deploy_network.py --diagnose` через 2–3 хв |
| `Permission denied (publickey)` | `chmod 400 cps-lab2-key.pem`; змінилась ваша IP → `--destroy` і розгорнути знову |
| Стенд: `address already in use 127.0.0.1:2222` | звільніть порт або змініть `stand.bastion_ssh_port` і `ports` у compose |
| Стенд: `Pool overlaps with other one` | у Docker уже є мережа 10.30.x: `docker network ls`, видаліть стару (`bash scripts/stand.sh down`) |
| LocalStack від ЛБ1 | `scripts/localstack.sh start` сам перезапустить його в режимі EC2 mock |
