# Маршрутизатор VPC + NAT Gateway стенду (Alpine, iptables)
FROM alpine:3.20
RUN apk add --no-cache iptables iproute2 curl
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
