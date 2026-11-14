"""
src/decide.py
-------------
Thuật toán ra quyết định di trú workload đề xuất (Proposed Strategy) tuân theo
hợp đồng `decision_function_protocol` trong `src/interfaces.py`.

Cơ chế chính:
- Tính lợi ích ròng đa mục tiêu B(d) = alpha * GG_norm - beta * NC_norm từ `objective.py`.
- Áp dụng vùng trễ Hysteresis (dead band) với ngưỡng mặc định theta = 0.15
  (khớp `configs/config.yaml -> pipeline.hysteresis_threshold`):
    * Chỉ MIGRATE khi max B(d) > theta.
    * Nếu 0 < max B(d) <= theta -> STAY (hysteresis_blocked=True) để chống ping-pong.
- Cung cấp `count_ping_pong`, `PingPongTracker`, `simulate_decisions` cho
  `pipeline.py` và kiểm thử đối chuẩn 3 chiến lược.
"""

import math
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # chạy trực tiếp `python src/xxx.py` hoặc `unittest discover -s src`
    from interfaces import (
        Action,
        DecisionResult,
        GreenForecastDict,
        NetworkMetricsDict,
        StrategyType,
    )
    from objective import (
        DEFAULT_OBJECTIVE_PARAMS,
        ObjectiveBreakdown,
        compute_objective,
        merge_objective_params,
    )
except ImportError:  # chạy dạng package `python -m src.xxx` từ gốc repo
    from src.interfaces import (
        Action,
        DecisionResult,
        GreenForecastDict,
        NetworkMetricsDict,
        StrategyType,
    )
    from src.objective import (
        DEFAULT_OBJECTIVE_PARAMS,
        ObjectiveBreakdown,
        compute_objective,
        merge_objective_params,
    )


DEFAULT_HYSTERESIS_THRESHOLD: float = 0.15
DEFAULT_DECISION_PARAMS: Dict[str, Any] = {
    **DEFAULT_OBJECTIVE_PARAMS,
    "hysteresis_threshold": DEFAULT_HYSTERESIS_THRESHOLD,
}

__all__ = [
    "DEFAULT_HYSTERESIS_THRESHOLD",
    "DEFAULT_DECISION_PARAMS",
    "decide_proposed",
    "count_ping_pong",
    "PingPongTracker",
    "simulate_decisions",
]


def _validate_non_negative_finite(value: Any, name: str) -> float:
    """Kiểm tra tham số là số hữu hạn >= 0 (loại trừ bool, NaN, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    num = float(value)
    if not math.isfinite(num) or num < 0.0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    return num


def decide_proposed(
    current_dc: str,
    green_forecast: GreenForecastDict,
    network_metrics: NetworkMetricsDict,
    workload_size_mb: float,
    params: Optional[Dict[str, Any]] = None,
) -> DecisionResult:
    """Ra quyết định STAY / MIGRATE theo tối ưu đa mục tiêu và ngưỡng Hysteresis."""
    if not isinstance(current_dc, str) or not current_dc:
        raise ValueError(f"current_dc must be a non-empty string, got {current_dc!r}")
    if not isinstance(green_forecast, Mapping) or current_dc not in green_forecast:
        raise ValueError(f"current_dc '{current_dc}' must exist in green_forecast")
    if not isinstance(network_metrics, Mapping):
        raise ValueError(f"network_metrics must be a mapping, got {type(network_metrics).__name__}")

    workload_mb = _validate_non_negative_finite(workload_size_mb, "workload_size_mb")
    if params is not None and not isinstance(params, Mapping):
        raise ValueError(f"params must be a mapping or None, got {type(params).__name__}")

    merged_obj = merge_objective_params(params)
    raw_theta = (
        DEFAULT_HYSTERESIS_THRESHOLD
        if params is None or params.get("hysteresis_threshold") is None
        else params.get("hysteresis_threshold")
    )
    theta = _validate_non_negative_finite(raw_theta, "hysteresis_threshold")

    current_green_raw = float(green_forecast[current_dc])
    if not math.isfinite(current_green_raw):
        raise ValueError(f"green_forecast[{current_dc!r}] must be finite, got {current_green_raw!r}")
    current_green_kw = max(0.0, current_green_raw)

    all_other_dcs = sorted((set(green_forecast) | set(network_metrics)) - {current_dc})
    candidates_details: Dict[str, Dict[str, Any]] = {}
    skipped: Dict[str, str] = {}
    feasible_bds: List[ObjectiveBreakdown] = []

    for dc in all_other_dcs:
        in_green = dc in green_forecast
        in_net = dc in network_metrics
        if not in_green:
            skipped[dc] = "missing green_forecast"
            continue
        if not in_net:
            skipped[dc] = "missing network_metrics"
            continue

        bd = compute_objective(
            current_dc=current_dc,
            target_dc=dc,
            green_forecast=green_forecast,
            network_metrics=network_metrics,
            workload_size_mb=workload_mb,
            params=merged_obj,
        )
        candidates_details[dc] = bd.to_dict()
        if not bd.feasible:
            skipped[dc] = "; ".join(bd.notes) if bd.notes else "infeasible network_metrics"
            continue
        feasible_bds.append(bd)

    best: Optional[ObjectiveBreakdown] = None
    hysteresis_blocked = False

    if not feasible_bds:
        action = Action.STAY
        target_dc = current_dc
        cost_score = 0.0
        reason = "STAY: no feasible candidate"
    else:
        max_net_benefit = max(bd.net_benefit for bd in feasible_bds)
        top_tied = [bd for bd in feasible_bds if (max_net_benefit - bd.net_benefit) < 1e-12]
        best = min(top_tied, key=lambda bd: (bd.network_cost_norm, bd.target_dc))

        if best.net_benefit > theta:
            action = Action.MIGRATE
            target_dc = best.target_dc
            cost_score = best.cost_score
            reason = (
                f"MIGRATE: {best.target_dc} net_benefit={best.net_benefit:.4f} "
                f"> threshold={theta:.4f}"
            )
        elif best.net_benefit > 0.0:
            action = Action.STAY
            target_dc = current_dc
            cost_score = 0.0
            hysteresis_blocked = True
            reason = (
                f"STAY: hysteresis blocked {best.target_dc} "
                f"(net_benefit={best.net_benefit:.4f} <= threshold={theta:.4f})"
            )
        else:
            action = Action.STAY
            target_dc = current_dc
            cost_score = 0.0
            reason = "STAY: no candidate with positive net benefit"

    target_green_kw = max(0.0, float(green_forecast[target_dc]))
    details: Dict[str, Any] = {
        "strategy_note": "multi-objective + hysteresis dead band",
        "alpha": merged_obj["alpha"],
        "beta": merged_obj["beta"],
        "hysteresis_threshold": theta,
        "workload_size_mb": workload_mb,
        "current_dc": current_dc,
        "current_green_kw": current_green_kw,
        "candidates": candidates_details,
        "skipped": skipped,
        "best_candidate": best.target_dc if best is not None else None,
        "best_net_benefit": best.net_benefit if best is not None else None,
        "hysteresis_blocked": hysteresis_blocked,
        "target_green_kw": target_green_kw,
    }

    return DecisionResult(
        action=action,
        target_dc=target_dc,
        strategy=StrategyType.PROPOSED,
        cost_score=cost_score,
        reason=reason,
        details=details,
    )


def count_ping_pong(dc_sequence: Sequence[str]) -> int:
    """Đếm số lần ping-pong sau khi nén các vị trí liên tiếp giống nhau.

    Một sự kiện ping-pong xảy ra khi workload quay lại đúng DC nó vừa rời ở bước
    chuyển ngay trước đó (y_i == y_{i-2} trên chuỗi đã nén).
    """
    compressed: List[str] = []
    for dc in dc_sequence:
        if not compressed or compressed[-1] != dc:
            compressed.append(dc)
    return sum(1 for i in range(2, len(compressed)) if compressed[i] == compressed[i - 2])


class PingPongTracker:
    """Bộ đếm số lần di trú và ping-pong tích lũy trong một lượt chạy thực nghiệm."""

    def __init__(self, initial_dc: str) -> None:
        if not isinstance(initial_dc, str) or not initial_dc:
            raise ValueError(f"initial_dc must be a non-empty string, got {initial_dc!r}")
        self._history: List[str] = [initial_dc]
        self._migrations: int = 0
        self._ping_pong_count: int = 0

    def update(self, new_location_dc: str) -> int:
        """Cập nhật vị trí DC sau một bước quyết định và trả về ping_pong_count tích lũy."""
        if not isinstance(new_location_dc, str) or not new_location_dc:
            raise ValueError(f"new_location_dc must be a non-empty string, got {new_location_dc!r}")
        if new_location_dc != self._history[-1]:
            self._migrations += 1
        self._history.append(new_location_dc)
        self._ping_pong_count = count_ping_pong(self._history)
        return self._ping_pong_count

    @property
    def migrations(self) -> int:
        """Tổng số lần thay đổi DC tính từ đầu lượt chạy."""
        return self._migrations

    @property
    def ping_pong_count(self) -> int:
        """Tổng số lần ping-pong tích lũy tính từ đầu lượt chạy."""
        return self._ping_pong_count

    @property
    def history(self) -> List[str]:
        """Bản sao chuỗi lịch sử vị trí DC đã ghi nhận."""
        return list(self._history)

    def reset(self, initial_dc: str) -> None:
        """Đặt lại trạng thái tracker cho lượt chạy mới."""
        if not isinstance(initial_dc, str) or not initial_dc:
            raise ValueError(f"initial_dc must be a non-empty string, got {initial_dc!r}")
        self._history = [initial_dc]
        self._migrations = 0
        self._ping_pong_count = 0


def simulate_decisions(
    decision_fn: Callable[..., DecisionResult],
    initial_dc: str,
    snapshots: Sequence[Tuple[GreenForecastDict, NetworkMetricsDict]],
    workload_size_mb: float,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mô phỏng chuỗi nhiều chu kỳ cho bất kỳ hàm nào tuân thủ decision_function_protocol."""
    tracker = PingPongTracker(initial_dc)
    current_dc = initial_dc
    locations: List[str] = []
    actions: List[Action] = []

    for green_forecast, network_metrics in snapshots:
        result = decision_fn(
            current_dc,
            green_forecast,
            network_metrics,
            workload_size_mb,
            params,
        )
        current_dc = result.target_dc
        tracker.update(current_dc)
        locations.append(current_dc)
        actions.append(result.action)

    return {
        "locations": locations,
        "actions": actions,
        "migrations": tracker.migrations,
        "ping_pong_count": tracker.ping_pong_count,
    }


if __name__ == "__main__":
    try:
        from interfaces import get_mock_green_forecast, get_mock_network_metrics
    except ImportError:
        from src.interfaces import get_mock_green_forecast, get_mock_network_metrics

    green = get_mock_green_forecast()
    net = get_mock_network_metrics()

    for size_mb in (100.0, 1000.0):
        res = decide_proposed("dc1", green, net, size_mb)
        print(
            f"[L={size_mb:.0f} MB] action={res.action.value} target={res.target_dc} "
            f"cost_score={res.cost_score:+.4f} blocked={res.details['hysteresis_blocked']} "
            f"reason={res.reason}"
        )
