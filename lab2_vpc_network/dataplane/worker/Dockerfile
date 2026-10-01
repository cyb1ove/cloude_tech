# Приватний OPC UA Industrial Server стенду (контекст збірки — корінь проєкту)
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server python3 python3-venv iproute2 iputils-ping netcat-openbsd traceroute curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/cps_opcua/venv && /opt/cps_opcua/venv/bin/pip install --no-cache-dir "asyncua>=1.1"
RUN useradd -m -s /bin/bash ubuntu && usermod -p '*' ubuntu && mkdir -p /run/sshd
COPY scripts/opcua_server.py /opt/cps_opcua/server.py
COPY dataplane/bastion/sshd_bastion.conf /etc/ssh/sshd_config.d/99-cps.conf
COPY dataplane/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
