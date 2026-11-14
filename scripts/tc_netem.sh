#!/usr/bin/env bash
# =====================================================================
# scripts/tc_netem.sh  —  Giả lập điều kiện mạng WAN cho 5 kịch bản
# Phụ trách: Nguyễn Đình Giang
#
# Cách dùng (chạy trên máy host, trong thư mục repo):
#   bash scripts/tc_netem.sh KB1|KB2|KB3|KB4|KB5   # áp kịch bản
#   bash scripts/tc_netem.sh clear                 # gỡ hết, trả lại mạng sạch
#   bash scripts/tc_netem.sh status                # xem đang áp gì
#
# Nguyên lý: netem được gắn trên router trung tâm r4, ở 2 card mạng:
#   - card thuộc wan_24 (10.10.24.0/24) = "đường tới DC2"
#   - card thuộc wan_34 (10.10.34.0/24) = "đường tới DC3"
# netem chỉ tác động lên chiều gói ĐI RA khỏi card (egress), tức chiều
# dữ liệu workload chảy từ DC nguồn (dc1) sang DC đích.
# =====================================================================
set -euo pipefail

R4="${R4_CONTAINER:-router-r4-firewall}"   # tên container r4 (khớp docker-compose.yml)
NET_DC2="10.10.24."                        # dải IP của link wan_24 (r4 <-> r2)
NET_DC3="10.10.34."                        # dải IP của link wan_34 (r4 <-> r3)

# Chạy một lệnh bên trong router r4
r4() { docker exec "$R4" "$@"; }

# Tìm tên card mạng của r4 có IP thuộc dải $1 (vd: eth0)
iface_of() {
  r4 ip -o -4 addr show | awk -v p="$1" 'index($4, p) == 1 { split($2, a, "@"); print a[1] }'
}

# Áp netem lên card $1 với các tham số còn lại; không có tham số = để đường sạch
apply() {
  local dev="$1"; shift
  r4 tc qdisc del dev "$dev" root 2>/dev/null || true   # xóa cấu hình cũ (nếu có)
  if [ "$#" -gt 0 ]; then
    r4 tc qdisc add dev "$dev" root netem "$@"
  fi
}

show_status() {
  echo "--- Đường tới DC2 ($IF_DC2) ---"; r4 tc qdisc show dev "$IF_DC2"
  echo "--- Đường tới DC3 ($IF_DC3) ---"; r4 tc qdisc show dev "$IF_DC3"
}

usage() {
  echo "Cách dùng: bash $0 {KB1|KB2|KB3|KB4|KB5|clear|status}" >&2
  exit 1
}

# ---- Kiểm tra đầu vào ----
[ "$#" -eq 1 ] || usage
SCENARIO="$(echo "$1" | tr '[:lower:]' '[:upper:]')"   # chấp nhận cả kb2 / KB2

IF_DC2="$(iface_of "$NET_DC2")"
IF_DC3="$(iface_of "$NET_DC3")"
if [ -z "$IF_DC2" ] || [ -z "$IF_DC3" ]; then
  echo "LỖI: không tìm thấy card wan_24/wan_34 trên $R4. Hạ tầng đã bật chưa? (docker compose up -d)" >&2
  exit 2
fi

# ---- Bảng tham số 5 kịch bản ----
#            Đường tới DC2 (nhiều nắng)                       | Đường tới DC3
case "$SCENARIO" in
  KB1)   # Mạng lý tưởng: không suy hao
    apply "$IF_DC2"
    apply "$IF_DC3" ;;
  KB2)   # Băng thông thấp -> nút thắt cổ chai (bottleneck) trên đường tới DC2
    apply "$IF_DC2" delay 20ms rate 10mbit
    apply "$IF_DC3" delay 10ms rate 100mbit ;;
  KB3)   # RTT cao + dao động trễ (jitter) + mất gói nhẹ trên đường tới DC2
    apply "$IF_DC2" delay 150ms 20ms distribution normal loss 0.5%
    apply "$IF_DC3" delay 20ms ;;
  KB4)   # Mạng trung bình, cố định; biến số là kích thước workload (pipeline đổi)
    apply "$IF_DC2" delay 30ms rate 50mbit
    apply "$IF_DC3" delay 30ms rate 50mbit ;;
  KB5)   # Mạng khá tốt; lưu lượng nền iperf3 do run_background_traffic.sh tạo
    apply "$IF_DC2" delay 20ms rate 100mbit
    apply "$IF_DC3" delay 20ms rate 100mbit ;;
  CLEAR)
    apply "$IF_DC2"
    apply "$IF_DC3" ;;
  STATUS)
    show_status; exit 0 ;;
  *) usage ;;
esac

echo "Đã áp kịch bản $SCENARIO trên $R4."
show_status
