# Bastion Host стенду: Ubuntu 22.04 (як AMI в AWS), користувач ubuntu, лише SSH
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server iproute2 iputils-ping netcat-openbsd traceroute curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash ubuntu && usermod -p '*' ubuntu && mkdir -p /run/sshd
COPY sshd_bastion.conf /etc/ssh/sshd_config.d/99-cps-bastion.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
