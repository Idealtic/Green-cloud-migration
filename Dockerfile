# Python 3.14 theo README.md (nền Debian để có đủ công cụ mạng)
FROM python:3.14-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Công cụ mạng theo phân công:
#  - iproute2 (ip, tc netem)     : routing + giả lập WAN (Giang)
#  - iputils-ping, traceroute    : đo RTT, kiểm tra đường đi
#  - iperf3                      : đo throughput, sinh lưu lượng nền KB5 (Thảo)
#  - tcpdump, tshark             : bắt gói, trích CWND/Retransmission (Phú)
#  - iptables, conntrack         : firewall trên r4 (Chi)
#  - netcat-openbsd, procps      : thử cổng TCP, pgrep/pkill cho pipeline
RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 iputils-ping traceroute net-tools \
        iptables conntrack \
        iperf3 tcpdump tshark \
        netcat-openbsd procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Cả repo được mount vào /app (xem docker-compose.yml), nên
# /app/configs/config.yaml và /app/src/*.py luôn là bản mới nhất.
CMD ["sleep", "infinity"]