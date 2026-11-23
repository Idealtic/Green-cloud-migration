#!/usr/bin/env bash
# =====================================================================
# scripts/run_background_traffic.sh — Sinh lưu lượng nền iperf3 (KB5)
# Phụ trách: Nguyễn Thị Thảo (nhánh feat/baselines-analytics)
#
# Mục đích:
#   Tạo lưu lượng nền cạnh tranh băng thông (Bandwidth Contention)
#   trên các đường link WAN nối tới Data Center đích (DC2, DC3)
#   cho kịch bản thực nghiệm KB5.
#
# Cách dùng (chạy trên máy host, trong thư mục repo):
#   bash scripts/run_background_traffic.sh start    # Bật lưu lượng nền
#   bash scripts/run_background_traffic.sh stop     # Dừng toàn bộ lưu lượng nền
#   bash scripts/run_background_traffic.sh status   # Kiểm tra trạng thái tiến trình
#
# Tùy chọn tham số nâng cao qua biến môi trường:
#   PORT=5202 BANDWIDTH=50M bash scripts/run_background_traffic.sh start
#
# Quy tắc kỹ thuật cốt lõi:
#   1. Cổng iperf3: Cố định cổng 5202 (hoặc qua biến $PORT).
#      TUYỆT ĐỐI KHÔNG dùng cổng 5201 vì đây là cổng độc quyền của
#      module probe trong src/pipeline.py để đo đạc thông lượng.
#   2. Loại lưu lượng: Sử dụng UDP Constant Bit Rate (CBR) với băng thông
#      mặc định 40 Mbps (chiếm ~40% dung lượng link 100 Mbps của KB5).
#      UDP không co cửa sổ nghẽn (CWND) như TCP, tạo áp lực tắc nghẽn
#      thực tế và kích hoạt trễ hàng đợi (Queueing Delay).
# =====================================================================
set -euo pipefail

TRAFFIC_PORT="${PORT:-5202}"
TRAFFIC_BW="${BANDWIDTH:-40M}"
SRC_CONTAINER="${SRC_DC:-dc1}"
DC2_CONTAINER="dc2"
DC3_CONTAINER="dc3"
DC2_IP="10.0.2.2"
DC3_IP="10.0.3.2"

check_container_running() {
  local name="$1"
  if ! docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q "true"; then
    echo "LỖI: Container '$name' chưa chạy! Hãy bật hạ tầng trước: docker compose up -d" >&2
    exit 2
  fi
}

check_all_containers() {
  check_container_running "$SRC_CONTAINER"
  check_container_running "$DC2_CONTAINER"
  check_container_running "$DC3_CONTAINER"
}

stop_traffic_quiet() {
  docker exec "$SRC_CONTAINER" pkill -f "iperf3.*${TRAFFIC_PORT}" 2>/dev/null || true
  docker exec "$DC2_CONTAINER" pkill -f "iperf3.*${TRAFFIC_PORT}" 2>/dev/null || true
  docker exec "$DC3_CONTAINER" pkill -f "iperf3.*${TRAFFIC_PORT}" 2>/dev/null || true
}

start_traffic() {
  check_all_containers
  echo "==> Đang dọn dẹp các tiến trình iperf3 cũ trên port ${TRAFFIC_PORT} (nếu có)..."
  stop_traffic_quiet
  sleep 1

  echo "==> Khởi động iperf3 server ngầm trên ${DC2_CONTAINER} và ${DC3_CONTAINER} (Port: ${TRAFFIC_PORT})..."
  docker exec -d "$DC2_CONTAINER" iperf3 -s -p "${TRAFFIC_PORT}"
  docker exec -d "$DC3_CONTAINER" iperf3 -s -p "${TRAFFIC_PORT}"
  sleep 1

  echo "==> Khởi động iperf3 UDP client ngầm từ ${SRC_CONTAINER} (Target: DC2=${DC2_IP}, DC3=${DC3_IP}, Băng thông: ${TRAFFIC_BW})..."
  docker exec -d "$SRC_CONTAINER" iperf3 -c "$DC2_IP" -p "${TRAFFIC_PORT}" -u -b "${TRAFFIC_BW}" -t 86400
  docker exec -d "$SRC_CONTAINER" iperf3 -c "$DC3_IP" -p "${TRAFFIC_PORT}" -u -b "${TRAFFIC_BW}" -t 86400
  sleep 1

  echo "==> Đã kích hoạt thành công lưu lượng nền cho KB5!"
  status_traffic
}

stop_traffic() {
  check_all_containers
  echo "==> Đang dừng lưu lượng nền trên port ${TRAFFIC_PORT}..."
  stop_traffic_quiet
  sleep 1
  echo "==> Đã dừng toàn bộ lưu lượng nền iperf3 port ${TRAFFIC_PORT}."
}

status_traffic() {
  check_all_containers
  echo "==================== TRẠNG THÁI LƯU LƯỢNG NỀN (PORT: ${TRAFFIC_PORT}) ===================="
  echo "[${DC2_CONTAINER}] Server:"
  docker exec "$DC2_CONTAINER" pgrep -fa "iperf3.*${TRAFFIC_PORT}" 2>/dev/null || echo "  (Không có tiến trình nào đang chạy)"
  echo "[${DC3_CONTAINER}] Server:"
  docker exec "$DC3_CONTAINER" pgrep -fa "iperf3.*${TRAFFIC_PORT}" 2>/dev/null || echo "  (Không có tiến trình nào đang chạy)"
  echo "[${SRC_CONTAINER}] Client (Sender):"
  docker exec "$SRC_CONTAINER" pgrep -fa "iperf3.*${TRAFFIC_PORT}" 2>/dev/null || echo "  (Không có tiến trình nào đang chạy)"
  echo "=========================================================================================="
}

usage() {
  echo "Cách dùng: bash $0 {start|stop|status}" >&2
  echo "Ví dụ:" >&2
  echo "  bash $0 start                   # Bật lưu lượng nền (mặc định 40M UDP)" >&2
  echo "  BANDWIDTH=60M bash $0 start     # Tùy chỉnh tốc độ tải 60 Mbps" >&2
  echo "  bash $0 status                  # Xem trạng thái lưu lượng nền" >&2
  echo "  bash $0 stop                    # Tắt toàn bộ lưu lượng nền" >&2
  exit 1
}

# ---- Kiểm tra tham số đầu vào ----
[ "$#" -eq 1 ] || usage
ACTION="$(echo "$1" | tr '[:upper:]' '[:lower:]')"

case "$ACTION" in
  start)
    start_traffic ;;
  stop)
    stop_traffic ;;
  status)
    status_traffic ;;
  *)
    usage ;;
esac
