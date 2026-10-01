"""
OPC UA Industrial Server — вузол КФС варіанта 3 (ЛБ2).

Імітує контролер виробничої лінії: публікує в адресному просторі OPC UA
змінні з телеметрією (температура, тиск, швидкість двигуна, стан), які
оновлюються щосекунди. Слухає opc.tcp://0.0.0.0:4840/cps/opcua/.

Той самий файл використовується:
  * в AWS — вбудовується в User Data приватного інстансу і запускається як systemd-сервіс;
  * на Docker-стенді (режим LocalStack) — копіюється в образ контейнера worker.

Параметри через змінні середовища: OPCUA_PORT, OPCUA_PATH, OPCUA_NS.
"""
import asyncio
import logging
import math
import os
import random
import socket

from asyncua import Server, ua

PORT = int(os.environ.get("OPCUA_PORT", "4840"))
PATH = os.environ.get("OPCUA_PATH", "/cps/opcua/")
NAMESPACE = os.environ.get("OPCUA_NS", "urn:cps:lab2:variant3")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cps-opcua")
log.setLevel(logging.INFO)


async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}{PATH}")
    server.set_server_name("CPS OPC UA Industrial Server (LB2, variant 3)")
    # У лабораторній мережі сервер доступний лише з Bastion (Security Group),
    # тому використовується політика без шифрування; у продуктиві — Basic256Sha256.
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    idx = await server.register_namespace(NAMESPACE)
    line = await server.nodes.objects.add_object(idx, "ProductionLine")
    temperature = await line.add_variable(idx, "Temperature_C", 20.0)
    pressure = await line.add_variable(idx, "Pressure_bar", 1.0)
    motor = await line.add_variable(idx, "MotorSpeed_rpm", 0.0)
    status = await line.add_variable(idx, "Status", "STARTING")
    host = await line.add_variable(idx, "Hostname", socket.gethostname())
    for node in (temperature, pressure, motor, status, host):
        await node.set_writable(False)

    log.info("OPC UA server listening on opc.tcp://0.0.0.0:%s%s (ns=%s)", PORT, PATH, idx)
    async with server:
        tick = 0
        await status.write_value("RUNNING")
        while True:
            await asyncio.sleep(1)
            tick += 1
            # Синтетична телеметрія: повільні коливання + шум
            await temperature.write_value(round(62.0 + 4.0 * math.sin(tick / 30) + random.uniform(-0.3, 0.3), 2))
            await pressure.write_value(round(4.2 + 0.3 * math.sin(tick / 45) + random.uniform(-0.05, 0.05), 3))
            await motor.write_value(round(1480.0 + 25.0 * math.sin(tick / 20) + random.uniform(-5, 5), 1))


if __name__ == "__main__":
    asyncio.run(main())
