"""
src/objective.py
----------------
Hàm mục tiêu chuẩn hóa đa biến (GreenGain vs NetworkCost) cho bài toán di trú
workload giữa các Cloud Data Center.

Đặc điểm thiết kế:
- Thuần tính toán (pure functions), không giữ trạng thái toàn cục, không I/O.
- Chuẩn hóa Min-Max với biên tham chiếu cố định để điểm số giữa các chu kỳ thời
  gian so sánh được với nhau, đảm bảo bộ lọc Hysteresis hoạt động ổn định.
- Toàn bộ đơn vị thống nhất theo chuẩn SI thập phân:
    * workload_size_mb: MB (1 MB = 10^6 byte = 8 * 10^6 bit)
    * throughput_mbps: Mbps (10^6 bit/s)
    * rtt_ms, queue_delay_ms: ms
    * packet_loss_rate: % (0.0 -> 100.0)
    * green_forecast: kW
"""

from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Mapping, Optional

try:  # chạy trực tiếp `python src/xxx.py` hoặc `unittest discover -s src`
    from interfaces import GreenForecastDict, NetworkMetrics, NetworkMetricsDict
except ImportError:  # chạy dạng package `python -m src.xxx` từ gốc repo
    from src.interfaces import GreenForecastDict, NetworkMetrics, NetworkMetricsDict


DEFAULT_OBJECTIVE_PARAMS: Dict[str, Any] = {
    "alpha": 0.5,
    "beta": 0.5,
    "green_ref_kw": 100.0,
    "migration_time_ref_s": 300.0,
    "rtt_ref_ms": 200.0,
    "loss_ref_pct": 10.0,
    "queue_ref_ms": 100.0,
    "w_time": 0.5,
    "w_rtt": 0.2,
    "w_loss": 0.2,
    "w_queue": 0.1,
    "rtt_overhead_count": 2,
    "use_mathis_bound": False,
    "mss_bytes": 1460,
    "mathis_c": 1.2247,
    "megabits_per_mb": 8.0,
}

__all__ = [
    "DEFAULT_OBJECTIVE_PARAMS",
    "ObjectiveBreakdown",
    "merge_objective_params",
    "min_max_normalize",
    "estimate_transmission_delay_s",
    "effective_throughput_mbps",
    "estimate_migration_time_s",
    "compute_green_gain_kw",
    "normalize_green_gain",
    "compute_network_cost",
    "compute_objective",
    "compute_break_even_workload_mb",
]


def _is_finite_number(value: Any) -> bool:
    """Kiểm tra giá trị là số thực/nguyên hữu hạn (loại trừ bool, NaN, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _require_finite(value: Any, name: str) -> float:
    """Ép kiểu float và báo ValueError nếu không phải số hữu hạn."""
    if not _is_finite_number(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _json_safe_float(value: Optional[float]) -> Optional[float]:
    """Đổi inf/NaN sang None để đầu ra to_dict() tuần tự hóa JSON an toàn."""
    if value is None or not math.isfinite(value):
        return None
    return float(value)


@dataclass
class ObjectiveBreakdown:
    """Kết quả phân rã chi tiết hàm mục tiêu khi xét chuyển từ current_dc tới target_dc."""

    current_dc: str
    target_dc: str
    feasible: bool
    green_gain_kw: float
    green_gain_norm: float
    effective_throughput_mbps: Optional[float]
    transmission_delay_s: Optional[float]
    est_migration_time_s: Optional[float]
    time_norm: float
    rtt_norm: float
    loss_norm: float
    queue_norm: float
    network_cost_norm: float
    net_benefit: float
    cost_score: float
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Chuyển sang dict tương thích JSON (không chứa inf hoặc NaN)."""
        return {
            "current_dc": self.current_dc,
            "target_dc": self.target_dc,
            "feasible": self.feasible,
            "green_gain_kw": _json_safe_float(self.green_gain_kw),
            "green_gain_norm": _json_safe_float(self.green_gain_norm),
            "effective_throughput_mbps": _json_safe_float(self.effective_throughput_mbps),
            "transmission_delay_s": _json_safe_float(self.transmission_delay_s),
            "est_migration_time_s": _json_safe_float(self.est_migration_time_s),
            "time_norm": _json_safe_float(self.time_norm),
            "rtt_norm": _json_safe_float(self.rtt_norm),
            "loss_norm": _json_safe_float(self.loss_norm),
            "queue_norm": _json_safe_float(self.queue_norm),
            "network_cost_norm": _json_safe_float(self.network_cost_norm),
            "net_benefit": _json_safe_float(self.net_benefit),
            "cost_score": _json_safe_float(self.cost_score),
            "notes": list(self.notes),
        }


def merge_objective_params(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Hợp nhất tham số cấu hình với giá trị mặc định, kiểm tra hợp lệ và chuẩn hóa trọng số.

    - Bỏ qua các khóa lạ (ví dụ ``hysteresis_threshold``, ``workload_size_mb``) để
      tương thích khi ``pipeline.py`` truyền nguyên cấu hình từ YAML.
    - Chuẩn hóa ``(alpha, beta)`` và ``(w_time, w_rtt, w_loss, w_queue)`` sao cho
      tổng mỗi nhóm bằng 1.0.
    """
    merged: Dict[str, Any] = dict(DEFAULT_OBJECTIVE_PARAMS)
    if params is not None:
        if not isinstance(params, Mapping):
            raise ValueError(f"params must be a mapping or None, got {type(params).__name__}")
        for key in DEFAULT_OBJECTIVE_PARAMS:
            if key in params and params[key] is not None:
                merged[key] = params[key]

    alpha = _require_finite(merged["alpha"], "alpha")
    beta = _require_finite(merged["beta"], "beta")
    if alpha < 0.0 or beta < 0.0 or (alpha + beta) <= 0.0:
        raise ValueError("alpha and beta must be >= 0 with alpha + beta > 0")
    ab_sum = alpha + beta
    merged["alpha"] = alpha / ab_sum
    merged["beta"] = beta / ab_sum

    for ref_key in (
        "green_ref_kw",
        "migration_time_ref_s",
        "rtt_ref_ms",
        "loss_ref_pct",
        "queue_ref_ms",
        "mss_bytes",
        "mathis_c",
        "megabits_per_mb",
    ):
        val = _require_finite(merged[ref_key], ref_key)
        if val <= 0.0:
            raise ValueError(f"{ref_key} must be > 0, got {val}")
        merged[ref_key] = val

    rtt_overhead = _require_finite(merged["rtt_overhead_count"], "rtt_overhead_count")
    if rtt_overhead < 0.0:
        raise ValueError(f"rtt_overhead_count must be >= 0, got {rtt_overhead}")
    merged["rtt_overhead_count"] = rtt_overhead

    w_keys = ("w_time", "w_rtt", "w_loss", "w_queue")
    weights = [_require_finite(merged[k], k) for k in w_keys]
    if any(w < 0.0 for w in weights) or sum(weights) <= 0.0:
        raise ValueError("network cost weights must be >= 0 with positive sum")
    w_sum = sum(weights)
    for key, w in zip(w_keys, weights):
        merged[key] = w / w_sum

    if not isinstance(merged["use_mathis_bound"], bool):
        raise ValueError(
            f"use_mathis_bound must be a bool, got {merged['use_mathis_bound']!r}"
        )

    return merged


def min_max_normalize(
    value: float,
    lower: float,
    upper: float,
    clip: bool = True,
) -> float:
    """Chuẩn hóa Min-Max giá trị ``value`` theo biên ``[lower, upper]`` về ``[0, 1]``."""
    v = _require_finite(value, "value")
    lo = _require_finite(lower, "lower")
    hi = _require_finite(upper, "upper")
    if hi <= lo:
        raise ValueError(f"upper ({hi}) must be strictly greater than lower ({lo})")
    norm = (v - lo) / (hi - lo)
    if clip:
        return max(0.0, min(1.0, norm))
    return norm


def estimate_transmission_delay_s(
    workload_size_mb: float,
    throughput_mbps: float,
    megabits_per_mb: float = 8.0,
) -> float:
    """Tính trễ truyền tải lý thuyết d_trans = (L * 8) / R (đơn vị: giây).

    Hàm này cũng được dùng cho bảng đối chiếu d_trans lý thuyết vs thực đo ở KB4.
    """
    size_mb = _require_finite(workload_size_mb, "workload_size_mb")
    if size_mb < 0.0:
        raise ValueError(f"workload_size_mb must be >= 0, got {size_mb}")
    bits_per_mb = _require_finite(megabits_per_mb, "megabits_per_mb")
    if bits_per_mb <= 0.0:
        raise ValueError(f"megabits_per_mb must be > 0, got {bits_per_mb}")

    if isinstance(throughput_mbps, bool) or not isinstance(throughput_mbps, (int, float)):
        raise ValueError(f"throughput_mbps must be numeric, got {throughput_mbps!r}")
    thr = float(throughput_mbps)
    if math.isnan(thr):
        raise ValueError("throughput_mbps must not be NaN")
    if thr <= 0.0:
        return math.inf
    return (size_mb * bits_per_mb) / thr


def effective_throughput_mbps(
    metrics: NetworkMetrics,
    params: Optional[Dict[str, Any]] = None,
) -> float:
    """Xác định thông lượng hiệu dụng (Mbps), tùy chọn áp cận trên Mathis et al. (1997)."""
    p = merge_objective_params(params)
    if not _is_finite_number(metrics.throughput_mbps) or float(metrics.throughput_mbps) <= 0.0:
        return 0.0
    measured_mbps = float(metrics.throughput_mbps)
    if not p["use_mathis_bound"]:
        return measured_mbps

    if (
        _is_finite_number(metrics.packet_loss_rate)
        and _is_finite_number(metrics.rtt_ms)
        and float(metrics.packet_loss_rate) > 0.0
        and float(metrics.rtt_ms) > 0.0
    ):
        rtt_s = float(metrics.rtt_ms) / 1000.0
        loss_frac = float(metrics.packet_loss_rate) / 100.0
        mathis_bps = (p["mss_bytes"] * 8.0 / rtt_s) * (p["mathis_c"] / math.sqrt(loss_frac))
        mathis_mbps = mathis_bps * 1e-6
        return min(measured_mbps, mathis_mbps)
    return measured_mbps


def _validate_metrics_for_feasibility(metrics: NetworkMetrics) -> List[str]:
    """Kiểm tra tính hợp lệ của các trường NetworkMetrics; trả về danh sách lý do lỗi."""
    reasons: List[str] = []
    if not _is_finite_number(metrics.throughput_mbps) or float(metrics.throughput_mbps) <= 0.0:
        reasons.append("throughput_mbps must be finite and > 0")
    if not _is_finite_number(metrics.rtt_ms) or float(metrics.rtt_ms) < 0.0:
        reasons.append("rtt_ms must be finite and >= 0")
    if (
        not _is_finite_number(metrics.packet_loss_rate)
        or float(metrics.packet_loss_rate) < 0.0
        or float(metrics.packet_loss_rate) > 100.0
    ):
        reasons.append("packet_loss_rate must be finite and in [0, 100]")
    if not _is_finite_number(metrics.queue_delay_ms) or float(metrics.queue_delay_ms) < 0.0:
        reasons.append("queue_delay_ms must be finite and >= 0")
    return reasons


def estimate_migration_time_s(
    workload_size_mb: float,
    metrics: NetworkMetrics,
    params: Optional[Dict[str, Any]] = None,
) -> float:
    """Ước lượng thời gian di trú T_mig = d_trans + k * RTT (giây); trả inf nếu không khả thi."""
    size_mb = _require_finite(workload_size_mb, "workload_size_mb")
    if size_mb < 0.0:
        raise ValueError(f"workload_size_mb must be >= 0, got {size_mb}")
    p = merge_objective_params(params)
    if _validate_metrics_for_feasibility(metrics):
        return math.inf
    r_eff = effective_throughput_mbps(metrics, p)
    if r_eff <= 0.0:
        return math.inf
    d_trans = estimate_transmission_delay_s(size_mb, r_eff, p["megabits_per_mb"])
    return d_trans + p["rtt_overhead_count"] * (float(metrics.rtt_ms) / 1000.0)


def compute_green_gain_kw(
    current_dc: str,
    target_dc: str,
    green_forecast: GreenForecastDict,
) -> float:
    """Tính chênh lệch công suất xanh G_target - G_current (kW), kẹp giá trị âm về 0."""
    if current_dc not in green_forecast:
        raise KeyError(f"current_dc '{current_dc}' missing from green_forecast")
    if target_dc not in green_forecast:
        raise KeyError(f"target_dc '{target_dc}' missing from green_forecast")

    raw_cur = _require_finite(green_forecast[current_dc], f"green_forecast[{current_dc!r}]")
    raw_tgt = _require_finite(green_forecast[target_dc], f"green_forecast[{target_dc!r}]")
    g_cur = max(0.0, raw_cur)
    g_tgt = max(0.0, raw_tgt)
    return g_tgt - g_cur


def normalize_green_gain(
    gain_kw: float,
    params: Optional[Dict[str, Any]] = None,
) -> float:
    """Chuẩn hóa GreenGain (kW) về [-1.0, 1.0] theo green_ref_kw."""
    p = merge_objective_params(params)
    gain = _require_finite(gain_kw, "gain_kw")
    norm = gain / p["green_ref_kw"]
    return max(-1.0, min(1.0, norm))


def compute_network_cost(
    workload_size_mb: float,
    metrics: NetworkMetrics,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Tính các thành phần chuẩn hóa và điểm chi phí mạng tổng hợp NetworkCost trong [0, 1]."""
    size_mb = _require_finite(workload_size_mb, "workload_size_mb")
    if size_mb < 0.0:
        raise ValueError(f"workload_size_mb must be >= 0, got {size_mb}")
    p = merge_objective_params(params)

    infeasible_reasons = _validate_metrics_for_feasibility(metrics)
    r_eff = 0.0 if infeasible_reasons else effective_throughput_mbps(metrics, p)
    if not infeasible_reasons and r_eff <= 0.0:
        infeasible_reasons.append("effective_throughput_mbps must be > 0")

    if infeasible_reasons:
        return {
            "time_norm": 1.0,
            "rtt_norm": 1.0,
            "loss_norm": 1.0,
            "queue_norm": 1.0,
            "network_cost_norm": 1.0,
            "est_migration_time_s": None,
            "transmission_delay_s": None,
            "effective_throughput_mbps": None,
            "feasible": False,
            "notes": infeasible_reasons,
        }

    d_trans = estimate_transmission_delay_s(size_mb, r_eff, p["megabits_per_mb"])
    t_mig = d_trans + p["rtt_overhead_count"] * (float(metrics.rtt_ms) / 1000.0)

    time_norm = min_max_normalize(t_mig, 0.0, p["migration_time_ref_s"], clip=True)
    rtt_norm = min_max_normalize(float(metrics.rtt_ms), 0.0, p["rtt_ref_ms"], clip=True)
    loss_norm = min_max_normalize(float(metrics.packet_loss_rate), 0.0, p["loss_ref_pct"], clip=True)
    queue_norm = min_max_normalize(float(metrics.queue_delay_ms), 0.0, p["queue_ref_ms"], clip=True)

    nc_norm = (
        p["w_time"] * time_norm
        + p["w_rtt"] * rtt_norm
        + p["w_loss"] * loss_norm
        + p["w_queue"] * queue_norm
    )
    nc_norm = max(0.0, min(1.0, nc_norm))

    return {
        "time_norm": time_norm,
        "rtt_norm": rtt_norm,
        "loss_norm": loss_norm,
        "queue_norm": queue_norm,
        "network_cost_norm": nc_norm,
        "est_migration_time_s": t_mig,
        "transmission_delay_s": d_trans,
        "effective_throughput_mbps": r_eff,
        "feasible": True,
        "notes": [],
    }


def compute_objective(
    current_dc: str,
    target_dc: str,
    green_forecast: GreenForecastDict,
    network_metrics: NetworkMetricsDict,
    workload_size_mb: float,
    params: Optional[Dict[str, Any]] = None,
) -> ObjectiveBreakdown:
    """Tính lợi ích ròng B(d) = alpha * GG_norm - beta * NC_norm và cost_score J(d) = -B(d)."""
    if current_dc not in green_forecast:
        raise KeyError(f"current_dc '{current_dc}' missing from green_forecast")
    if target_dc not in green_forecast:
        raise KeyError(f"target_dc '{target_dc}' missing from green_forecast")
    if target_dc not in network_metrics:
        raise KeyError(f"target_dc '{target_dc}' missing from network_metrics")

    p = merge_objective_params(params)
    notes: List[str] = []

    raw_cur = _require_finite(green_forecast[current_dc], f"green_forecast[{current_dc!r}]")
    raw_tgt = _require_finite(green_forecast[target_dc], f"green_forecast[{target_dc!r}]")
    if raw_cur < 0.0:
        notes.append(f"clamped negative green_forecast for {current_dc} from {raw_cur} to 0.0")
    if raw_tgt < 0.0:
        notes.append(f"clamped negative green_forecast for {target_dc} from {raw_tgt} to 0.0")

    gg_kw = compute_green_gain_kw(current_dc, target_dc, green_forecast)
    gg_norm = normalize_green_gain(gg_kw, p)
    nc = compute_network_cost(workload_size_mb, network_metrics[target_dc], p)
    notes.extend(nc["notes"])

    if not nc["feasible"]:
        net_benefit = -math.inf
        cost_score = math.inf
    else:
        net_benefit = p["alpha"] * gg_norm - p["beta"] * nc["network_cost_norm"]
        cost_score = -net_benefit

    return ObjectiveBreakdown(
        current_dc=current_dc,
        target_dc=target_dc,
        feasible=nc["feasible"],
        green_gain_kw=gg_kw,
        green_gain_norm=gg_norm,
        effective_throughput_mbps=nc["effective_throughput_mbps"],
        transmission_delay_s=nc["transmission_delay_s"],
        est_migration_time_s=nc["est_migration_time_s"],
        time_norm=nc["time_norm"],
        rtt_norm=nc["rtt_norm"],
        loss_norm=nc["loss_norm"],
        queue_norm=nc["queue_norm"],
        network_cost_norm=nc["network_cost_norm"],
        net_benefit=net_benefit,
        cost_score=cost_score,
        notes=notes,
    )


def compute_break_even_workload_mb(
    current_dc: str,
    target_dc: str,
    green_forecast: GreenForecastDict,
    network_metrics: NetworkMetricsDict,
    threshold: float = 0.0,
    params: Optional[Dict[str, Any]] = None,
    max_workload_mb: float = 100000.0,
    tol_mb: float = 0.01,
) -> Optional[float]:
    """Tìm kích thước workload hòa vốn L* (MB) sao cho B(target_dc; L*) = threshold bằng bisection.

    Trả về:
    - 0.0 nếu ngay tại L = 0 đã có B <= threshold.
    - None nếu liên kết không khả thi hoặc tại L = max_workload_mb vẫn có B > threshold.
    """
    thr_val = _require_finite(threshold, "threshold")
    max_mb = _require_finite(max_workload_mb, "max_workload_mb")
    tol = _require_finite(tol_mb, "tol_mb")
    if max_mb <= 0.0:
        raise ValueError(f"max_workload_mb must be > 0, got {max_mb}")
    if tol <= 0.0:
        raise ValueError(f"tol_mb must be > 0, got {tol}")

    p = merge_objective_params(params)
    bd_zero = compute_objective(
        current_dc, target_dc, green_forecast, network_metrics, 0.0, p
    )
    if not bd_zero.feasible:
        return None
    if bd_zero.net_benefit <= thr_val:
        return 0.0

    bd_max = compute_objective(
        current_dc, target_dc, green_forecast, network_metrics, max_mb, p
    )
    if bd_max.net_benefit > thr_val:
        return None

    lo = 0.0
    hi = max_mb
    for _ in range(200):
        if (hi - lo) <= tol:
            break
        mid = 0.5 * (lo + hi)
        bd_mid = compute_objective(
            current_dc, target_dc, green_forecast, network_metrics, mid, p
        )
        if bd_mid.net_benefit > thr_val:
            lo = mid
        else:
            hi = mid

    return 0.5 * (lo + hi)


if __name__ == "__main__":
    try:
        from interfaces import get_mock_green_forecast, get_mock_network_metrics
    except ImportError:
        from src.interfaces import get_mock_green_forecast, get_mock_network_metrics

    green = get_mock_green_forecast()
    net = get_mock_network_metrics()
    workload_mb = 100.0

    print("=== Objective Breakdown (current_dc=dc1, workload=100 MB) ===")
    for dc in ("dc2", "dc3"):
        bd = compute_objective("dc1", dc, green, net, workload_mb)
        print(
            f"{dc}: GG={bd.green_gain_kw:+.1f} kW (norm={bd.green_gain_norm:+.4f}), "
            f"d_trans={bd.transmission_delay_s:.2f} s, T_mig={bd.est_migration_time_s:.2f} s, "
            f"NC_norm={bd.network_cost_norm:.6f}, B={bd.net_benefit:+.6f}, J={bd.cost_score:+.6f}"
        )

    l_hyst = compute_break_even_workload_mb("dc1", "dc2", green, net, threshold=0.15)
    l_greedy = compute_break_even_workload_mb("dc1", "dc2", green, net, threshold=0.0)
    print(f"Break-even dc1 -> dc2 (theta=0.15): {l_hyst:.4f} MB")
    print(f"Break-even dc1 -> dc2 (theta=0.00): {l_greedy:.4f} MB")
