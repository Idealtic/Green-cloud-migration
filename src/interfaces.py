"""
src/interfaces.py
-----------------
BỘ KHUNG GIAO TIẾP DỮ LIỆU CHUẨN (INTERFACE CONTRACT)
Đề tài: Nghiên cứu ứng dụng ML dự đoán năng lượng tái tạo kết hợp đánh giá mạng hỗ trợ di trú workload.

Mục đích:
1. Chuẩn hóa định dạng và tên cột cho 5 file CSV thực nghiệm (kb1.csv -> kb5.csv) để analyze.py đọc chung.
2. Thống nhất chữ ký hàm (Function Signature) giữa decide.py (Hoàng) và baselines.py (Thảo).
3. Định nghĩa chuẩn đầu ra của predict.py (Dương) cho Baseline A và Proposed.
4. Cung cấp dữ liệu mẫu (mock data) để các thành viên lập trình độc lập mà không bị block.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Optional, List


# ==============================================================================
# 1. ĐỊNH DẠNG FILE CSV CHUẨN CHO 5 KỊCH BẢN THỰC NGHIỆM (kb1.csv -> kb5.csv)
# ==============================================================================
# Toàn bộ kết quả thực nghiệm của Huệ Chi (KB1), Đình Giang (KB2),
# Đặng Thị Thùy Dương (KB3), Phạm Minh Hoàng (KB4), Nguyễn Thị Thảo (KB5)
# BẮT BUỘC phải ghi theo đúng thứ tự và tên các cột sau để analyze.py đọc tự động:

CSV_HEADER: List[str] = [
    "timestamp",           # Thời điểm ghi log (ISO 8601 string hoặc Unix epoch)
    "iteration",           # Lần lặp thử nghiệm: 1 -> 5 (int)
    "scenario",            # Tên kịch bản: "KB1", "KB2", "KB3", "KB4", "KB5" (str)
    "strategy",            # Chiến lược: "Proposed", "Baseline_A", "Baseline_B" (str)
    "current_dc",          # Data Center hiện tại: "dc1", "dc2", "dc3" (str)
    "decision",            # Quyết định: "STAY" hoặc "MIGRATE" (str)
    "target_dc",           # DC đích: "dc1", "dc2", "dc3" (nếu STAY thì target_dc == current_dc)
    "workload_size_mb",    # Kích thước workload di trú (đơn vị: MB, float)
    "rtt_ms",              # Round Trip Time đến DC đích (đơn vị: ms, float)
    "throughput_mbps",     # Băng thông đo được trong quá trình truyền (đơn vị: Mbps, float)
    "migration_time_s",    # Thời gian hoàn thành truyền workload (đơn vị: giây, float)
    "packet_loss_rate",    # Tỷ lệ mất gói tin (đơn vị: %, float từ 0.0 đến 100.0)
    "green_energy_kw",     # Công suất xanh tại DC đích theo dự báo (đơn vị: kW, float)
    "ping_pong_count"      # Số lần đảo quyết định liên tiếp (int)
]


# ==============================================================================
# 2. CÁC HẰNG SỐ VÀ ENUM ĐIỀU KHIỂN
# ==============================================================================
class Action(str, Enum):
    """Quyết định hành động di trú."""
    STAY = "STAY"
    MIGRATE = "MIGRATE"


class StrategyType(str, Enum):
    """Ba chiến lược đối chuẩn bắt buộc của đề tài."""
    PROPOSED = "Proposed"      # Chiến lược đề xuất: Tối ưu đa mục tiêu + Hysteresis
    BASELINE_A = "Baseline_A"  # Tham lam Năng lượng xanh: Chọn DC xanh nhất, KHÔNG dùng Hysteresis
    BASELINE_B = "Baseline_B"  # Tham lam Mạng: Chọn DC mạng tốt nhất (RTT thấp nhất), KHÔNG dùng Hysteresis


# ==============================================================================
# 3. CẤU TRÚC DỮ LIỆU ĐẦU VÀO (INPUT DATA SCHEMAS)
# ==============================================================================
@dataclass
class NetworkMetrics:
    """Số đo điều kiện mạng từ DC nguồn đến một DC đích cụ thể."""
    target_dc: str
    rtt_ms: float               # Độ trễ vòng lặp (ms)
    throughput_mbps: float      # Băng thông khả dụng (Mbps)
    packet_loss_rate: float     # Tỉ lệ mất gói (%)
    queue_delay_ms: float = 0.0 # Trễ hàng đợi ước lượng (ms)


@dataclass
class DecisionResult:
    """
    Kết quả trả về của hàm ra quyết định (decide.py & baselines.py).
    Huệ Chi (pipeline.py) sẽ nhận đối tượng này để điều phối socket truyền file.
    """
    action: Action              # Action.STAY hoặc Action.MIGRATE
    target_dc: str              # Tên DC đích (ví dụ: "dc2")
    strategy: StrategyType      # Tên chiến lược đã chạy
    cost_score: float = 0.0     # Điểm số hàm mục tiêu tính được (nếu có)
    reason: str = ""            # Ghi chú lý do ra quyết định
    details: Dict[str, Any] = field(default_factory=dict) # Chi tiết GreenGain, NetworkCost, Hysteresis


# ==============================================================================
# 4. CHỮ KÝ HÀM CHUẨN (FUNCTION SIGNATURES)
# ==============================================================================
# [A] ĐỊNH DẠNG ĐẦU RA CỦA predict.py (Đặng Thị Thùy Dương):
# Hàm predict_green_energy(timestamp: float) phải trả về Dict[str, float]
# Ánh xạ tên Data Center -> Công suất xanh dự báo (kW):
# Ví dụ: {"dc1": 45.2, "dc2": 88.5, "dc3": 12.0}
GreenForecastDict = Dict[str, float]

# [B] ĐỊNH DẠNG ĐẦU VÀO CỦA ĐO ĐẠC MẠNG:
# Ánh xạ tên DC đích -> NetworkMetrics tương ứng:
NetworkMetricsDict = Dict[str, NetworkMetrics]


def decision_function_protocol(
    current_dc: str,
    green_forecast: GreenForecastDict,
    network_metrics: NetworkMetricsDict,
    workload_size_mb: float,
    params: Optional[Dict[str, Any]] = None
) -> DecisionResult:
    """
    CHỮ KÝ HÀM MẪU DÀNH CHO:
    - Hoàng (src/decide.py -> hàm decide_proposed)
    - Thảo (src/baselines.py -> hàm decide_baseline_a & decide_baseline_b)

    Tham số:
    - current_dc: DC mà workload đang chạy hiện tại ("dc1", "dc2", hoặc "dc3").
    - green_forecast: Dict công suất xanh dự báo từ predict.py (kW).
    - network_metrics: Dict thông số mạng tới các DC đích từ module socket/ping.
    - workload_size_mb: Kích thước workload cần di trú (MB).
    - params: Các tham số cấu hình (alpha, beta, hysteresis_threshold,...).

    Trả về:
    - DecisionResult (chứa action: STAY/MIGRATE và target_dc).
    
    LƯU Ý VỀ HYSTERESIS:
    - Baseline_A và Baseline_B: KHÔNG SỬ DỤNG Hysteresis (thuần tham lam).
    - Proposed (Hoàng): BẮT BUỘC dùng Hysteresis Threshold để chống rung lắc (ping-pong).
    """
    raise NotImplementedError("Chỉ là giao diện mẫu. Hãy implement trong decide.py và baselines.py.")


# ==============================================================================
# 5. DỮ LIỆU MẪU (MOCK DATA) CHO LẬP TRÌNH ĐỘC LẬP
# ==============================================================================
def get_mock_green_forecast() -> GreenForecastDict:
    """Hàm cung cấp dữ liệu xanh giả lập để test trước khi model.pkl sẵn sàng."""
    return {
        "dc1": 35.0,  # kW
        "dc2": 85.0,  # kW (Xanh nhất)
        "dc3": 20.0   # kW
    }


def get_mock_network_metrics() -> NetworkMetricsDict:
    """Hàm cung cấp dữ liệu mạng giả lập để test trước khi hạ tầng sẵn sàng."""
    return {
        "dc1": NetworkMetrics(target_dc="dc1", rtt_ms=0.5, throughput_mbps=1000.0, packet_loss_rate=0.0),
        "dc2": NetworkMetrics(target_dc="dc2", rtt_ms=45.0, throughput_mbps=50.0, packet_loss_rate=1.5),
        "dc3": NetworkMetrics(target_dc="dc3", rtt_ms=15.0, throughput_mbps=100.0, packet_loss_rate=0.1)
    }


def create_csv_row(
    timestamp: str,
    iteration: int,
    scenario: str,
    strategy: str,
    current_dc: str,
    decision: str,
    target_dc: str,
    workload_size_mb: float,
    rtt_ms: float,
    throughput_mbps: float,
    migration_time_s: float,
    packet_loss_rate: float,
    green_energy_kw: float,
    ping_pong_count: int = 0
) -> Dict[str, Any]:
    """Helper đảm bảo mọi thành viên khi ghi file CSV đều đủ các trường theo CSV_HEADER."""
    return {
        "timestamp": timestamp,
        "iteration": iteration,
        "scenario": scenario,
        "strategy": strategy,
        "current_dc": current_dc,
        "decision": decision,
        "target_dc": target_dc,
        "workload_size_mb": workload_size_mb,
        "rtt_ms": rtt_ms,
        "throughput_mbps": throughput_mbps,
        "migration_time_s": migration_time_s,
        "packet_loss_rate": packet_loss_rate,
        "green_energy_kw": green_energy_kw,
        "ping_pong_count": ping_pong_count
    }
