"""
src/baselines.py
----------------
Hai thuật toán đối chuẩn (Baseline) của đề tài, tuân theo chữ ký hàm trong
src/interfaces.py (decision_function_protocol):

    - decide_baseline_a : tham lam NĂNG LƯỢNG XANH  -> chọn DC có công suất xanh dự báo cao nhất.
    - decide_baseline_b : tham lam MẠNG             -> chọn DC có RTT thấp nhất.

Cả hai KHÔNG dùng Hysteresis (thuần tham lam), nên dễ bị rung lắc (ping-pong)
khi số liệu dao động; đây chính là điểm để so sánh với chiến lược Proposed.

Quy ước chung:
    - Ứng viên gồm mọi DC có trong green_forecast (kể cả DC hiện tại).
    - Nếu DC tốt nhất chính là DC hiện tại -> STAY, ngược lại -> MIGRATE.
    - Khi hòa điểm, ưu tiên DC hiện tại (tránh di trú vô ích), sau đó theo tên DC
      để kết quả luôn lặp lại được (deterministic).
"""

from typing import Any, Dict, Optional

try:  # chạy bằng `python src/xxx.py` (thư mục src nằm trên sys.path)
    from interfaces import (
        Action,
        DecisionResult,
        GreenForecastDict,
        NetworkMetricsDict,
        StrategyType,
        get_mock_green_forecast,
        get_mock_network_metrics,
    )
except ImportError:  # chạy dạng package: `python -m src.xxx` hoặc import từ gốc repo
    from src.interfaces import (
        Action,
        DecisionResult,
        GreenForecastDict,
        NetworkMetricsDict,
        StrategyType,
        get_mock_green_forecast,
        get_mock_network_metrics,
    )


def _validate(current_dc: str, green_forecast: GreenForecastDict) -> None:
    if not green_forecast:
        raise ValueError("green_forecast rỗng: không có DC nào để lựa chọn.")
    if current_dc not in green_forecast:
        raise ValueError(
            f"current_dc='{current_dc}' không có trong green_forecast {sorted(green_forecast)}."
        )


def _build_result(
    current_dc: str,
    best_dc: str,
    strategy: StrategyType,
    cost_score: float,
    reason: str,
    details: Dict[str, Any],
) -> DecisionResult:
    action = Action.STAY if best_dc == current_dc else Action.MIGRATE
    return DecisionResult(
        action=action,
        target_dc=best_dc,  # STAY thì target_dc == current_dc, đúng quy ước CSV
        strategy=strategy,
        cost_score=cost_score,
        reason=reason,
        details=details,
    )


# ==============================================================================
# BASELINE A - THAM LAM NĂNG LƯỢNG XANH (bỏ qua hoàn toàn điều kiện mạng)
# ==============================================================================
def decide_baseline_a(
    current_dc: str,
    green_forecast: GreenForecastDict,
    network_metrics: NetworkMetricsDict,
    workload_size_mb: float,
    params: Optional[Dict[str, Any]] = None,
) -> DecisionResult:
    """Chọn DC có công suất xanh dự báo (kW) cao nhất. Không xét mạng, không Hysteresis."""
    _validate(current_dc, green_forecast)

    # key: xanh nhiều nhất -> ưu tiên DC hiện tại khi hòa -> theo tên DC
    best_dc = min(
        green_forecast,
        key=lambda dc: (-green_forecast[dc], dc != current_dc, dc),
    )
    best_kw = green_forecast[best_dc]
    reason = (
        f"Baseline A: {best_dc} xanh nhất ({best_kw:.1f} kW), "
        f"DC hiện tại {current_dc} ({green_forecast[current_dc]:.1f} kW)."
    )
    return _build_result(
        current_dc,
        best_dc,
        StrategyType.BASELINE_A,
        cost_score=-best_kw,  # càng xanh điểm chi phí càng thấp (âm)
        reason=reason,
        details={
            "green_forecast": dict(green_forecast),
            "chosen_green_kw": best_kw,
            "workload_size_mb": workload_size_mb,
            "hysteresis": False,
        },
    )


# ==============================================================================
# BASELINE B - THAM LAM MẠNG (bỏ qua hoàn toàn năng lượng xanh)
# ==============================================================================
def decide_baseline_b(
    current_dc: str,
    green_forecast: GreenForecastDict,
    network_metrics: NetworkMetricsDict,
    workload_size_mb: float,
    params: Optional[Dict[str, Any]] = None,
) -> DecisionResult:
    """Chọn DC có RTT thấp nhất (hòa thì throughput cao hơn). Không xét xanh, không Hysteresis.

    Nếu network_metrics không có số đo tới current_dc (DC nguồn đo tới chính nó),
    coi RTT = 0 ms, nghĩa là ở lại DC hiện tại luôn là phương án mạng tốt nhất.
    """
    _validate(current_dc, green_forecast)

    def rtt_of(dc: str) -> float:
        m = network_metrics.get(dc)
        if m is None:
            if dc == current_dc:
                return 0.0
            return float("inf")  # không có số đo mạng -> không chọn
        return m.rtt_ms

    def thr_of(dc: str) -> float:
        m = network_metrics.get(dc)
        return m.throughput_mbps if m is not None else 0.0

    candidates = list(green_forecast)
    best_dc = min(
        candidates,
        key=lambda dc: (rtt_of(dc), -thr_of(dc), dc != current_dc, dc),
    )
    best_rtt = rtt_of(best_dc)
    if best_rtt == float("inf"):
        raise ValueError("network_metrics không có số đo cho bất kỳ DC ứng viên nào.")

    reason = f"Baseline B: {best_dc} có RTT thấp nhất ({best_rtt:.1f} ms)."
    return _build_result(
        current_dc,
        best_dc,
        StrategyType.BASELINE_B,
        cost_score=best_rtt,  # RTT càng thấp càng tốt
        reason=reason,
        details={
            "rtt_ms": {dc: rtt_of(dc) for dc in candidates},
            "chosen_rtt_ms": best_rtt,
            "workload_size_mb": workload_size_mb,
            "hysteresis": False,
        },
    )


# ==============================================================================
# TỰ KIỂM TRA NHANH:  python src/baselines.py
# ==============================================================================
if __name__ == "__main__":
    green = get_mock_green_forecast()  # dc2 xanh nhất (85 kW)
    net = get_mock_network_metrics()  # dc1 RTT thấp nhất (0.5 ms)

    # 1) Từ dc1: A phải MIGRATE sang dc2, B phải STAY ở dc1
    a = decide_baseline_a("dc1", green, net, 500.0)
    b = decide_baseline_b("dc1", green, net, 500.0)
    assert (a.action, a.target_dc) == (Action.MIGRATE, "dc2"), a
    assert (b.action, b.target_dc) == (Action.STAY, "dc1"), b

    # 2) Từ dc2: A phải STAY (đã ở DC xanh nhất), B phải MIGRATE sang dc1
    a = decide_baseline_a("dc2", green, net, 500.0)
    b = decide_baseline_b("dc2", green, net, 500.0)
    assert (a.action, a.target_dc) == (Action.STAY, "dc2"), a
    assert (b.action, b.target_dc) == (Action.MIGRATE, "dc1"), b

    # 3) Hòa điểm xanh -> ưu tiên ở lại DC hiện tại
    tie = {"dc1": 50.0, "dc2": 50.0, "dc3": 10.0}
    assert decide_baseline_a("dc2", tie, net, 100.0).action == Action.STAY

    # 4) Đầu vào sai phải báo lỗi rõ ràng
    for bad in ({}, {"dc9": 1.0}):
        try:
            decide_baseline_a("dc1", bad, net, 100.0)
            raise SystemExit("Lẽ ra phải báo lỗi với đầu vào sai")
        except ValueError:
            pass

    print("OK: baselines.py chạy đúng trên dữ liệu mẫu.")
    print(" ", decide_baseline_a("dc1", green, net, 500.0).reason)
    print(" ", decide_baseline_b("dc2", green, net, 500.0).reason)