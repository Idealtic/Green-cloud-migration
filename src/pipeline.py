"""
src/pipeline.py
---------------
Bộ điều phối thực nghiệm của đề tài. Với mỗi kịch bản KB1..KB5 chạy vòng lặp:

    áp netem  ->  [mỗi chu kỳ] dự báo năng lượng xanh + đo mạng
              ->  3 chiến lược (Proposed, Baseline_A, Baseline_B) ra quyết định
              ->  nếu MIGRATE thì truyền workload (sender/receiver)
              ->  ghi 1 dòng CSV theo CSV_HEADER (interfaces.py)

Cả 3 chiến lược chạy "song song" trên CÙNG một snapshot (cùng dự báo xanh, cùng số đo
mạng) nên so sánh công bằng. Mỗi chiến lược giữ vị trí workload và bộ đếm ping-pong riêng.

Cách chạy (từ thư mục gốc repo):
    python src/pipeline.py                          # mock, cả 5 kịch bản (không cần Docker)
    python src/pipeline.py --scenarios KB2 KB3      # chỉ vài kịch bản
    python src/pipeline.py --mode docker            # đo/truyền thật trong container dc1..dc3
    python src/pipeline.py --mode docker --scenarios KB1 --iterations 1 --snapshots 2

Cấu hình đọc từ configs/config.yaml (IP, role, workload, hysteresis) và
configs/pipeline_config.yaml (tham số thực nghiệm). Kết quả: results/csv/kbN.csv
(mock: kbN_mock.csv). Phân tích tiếp bằng src/analyze.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import pi, sin
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import yaml

try:  # chạy bằng `python src/pipeline.py`
    from baselines import decide_baseline_a, decide_baseline_b
    from decide import PingPongTracker, decide_proposed
    from interfaces import (
        CSV_HEADER,
        Action,
        DecisionResult,
        GreenForecastDict,
        NetworkMetrics,
        NetworkMetricsDict,
        create_csv_row,
    )
except ImportError:  # chạy dạng package `python -m src.pipeline`
    from src.baselines import decide_baseline_a, decide_baseline_b
    from src.decide import PingPongTracker, decide_proposed
    from src.interfaces import (
        CSV_HEADER,
        Action,
        DecisionResult,
        GreenForecastDict,
        NetworkMetrics,
        NetworkMetricsDict,
        create_csv_row,
    )

logger = logging.getLogger("pipeline")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "configs" / "config.yaml"
PIPELINE_CONFIG_PATH = REPO_ROOT / "configs" / "pipeline_config.yaml"
DEFAULT_NETEM_CMD = "bash scripts/tc_netem.sh {scenario}"

SCENARIOS = ["KB1", "KB2", "KB3", "KB4", "KB5"]
DECIDERS: Dict[str, Callable[..., DecisionResult]] = {
    "Proposed": decide_proposed,
    "Baseline_A": decide_baseline_a,
    "Baseline_B": decide_baseline_b,
}


# ==============================================================================
# 1. CẤU HÌNH
# ==============================================================================
def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        sys.exit(f"[LỖI] Không tìm thấy file cấu hình: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Settings:
    """Gộp config.yaml + pipeline_config.yaml thành một chỗ dễ dùng."""

    dcs: Dict[str, Dict[str, Any]]
    source_dc: str
    workload_size_mb: float
    interval_s: float
    hysteresis_threshold: float
    pc: Dict[str, Any]  # toàn bộ pipeline_config.yaml

    @classmethod
    def load(cls) -> "Settings":
        base = load_yaml(CONFIG_PATH)
        pc = load_yaml(PIPELINE_CONFIG_PATH)
        dcs = base["data_centers"]
        sources = [d for d, v in dcs.items() if v.get("role") == "source"]
        if len(sources) != 1:
            sys.exit(f"[LỖI] config.yaml cần đúng 1 DC role=source, hiện có: {sources}")
        p = base["pipeline"]
        return cls(
            dcs=dcs,
            source_dc=sources[0],
            workload_size_mb=float(p["workload_size_mb"]),
            interval_s=float(p["interval_seconds"]),
            hysteresis_threshold=float(p["hysteresis_threshold"]),
            pc=pc,
        )

    @property
    def dc_ids(self) -> List[str]:
        return sorted(self.dcs)

    def ip_of(self, dc: str) -> str:
        return str(self.dcs[dc]["ip"])


# ==============================================================================
# 2. DỰ BÁO NĂNG LƯỢNG XANH
# ==============================================================================
def _mock_green_forecast(ts: float, dc_ids: Sequence[str], rng: random.Random) -> GreenForecastDict:
    """Dự báo giả (mặt trời + gió) khi chưa có predict.py / model.pkl. KHÔNG phải kết quả ML."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    hour = dt.hour + dt.minute / 60.0
    solar = max(0.0, sin(pi * (hour - 6.0) / 12.0))  # 6h..18h
    # (công suất mặt trời tối đa, công suất gió tối đa, pha gió) cho từng DC
    profiles = {"dc1": (40.0, 15.0, 0.0), "dc2": (90.0, 5.0, 3.0), "dc3": (15.0, 40.0, 9.0)}
    out: GreenForecastDict = {}
    for dc in dc_ids:
        solar_cap, wind_cap, phase = profiles.get(dc, (30.0, 10.0, 0.0))
        wind = 0.6 + 0.4 * sin(2 * pi * (hour + phase) / 24.0)
        out[dc] = max(0.0, (solar_cap * solar + wind_cap * wind) * (1.0 + rng.uniform(-0.1, 0.1)))
    return out


class GreenSource:
    """Lấy dự báo xanh từ predict.predict_green_energy(timestamp); thiếu thì dùng bản giả."""

    def __init__(self, dc_ids: Sequence[str], seed: int, force_mock: bool = False) -> None:
        self.dc_ids = list(dc_ids)
        self.seed = seed
        self._fn: Optional[Callable[..., GreenForecastDict]] = None
        if not force_mock:
            self._fn = self._load_predictor()
        if self._fn is None:
            logger.warning("Chưa có predict_green_energy -> dùng dự báo xanh GIẢ (mock).")

    @staticmethod
    def _load_predictor() -> Optional[Callable[..., GreenForecastDict]]:
        for mod in ("predict", "src.predict"):
            try:
                module = __import__(mod, fromlist=["predict_green_energy"])
                return getattr(module, "predict_green_energy")
            except (ImportError, AttributeError):
                continue
        return None

    @property
    def is_mock(self) -> bool:
        return self._fn is None

    def forecast(self, sim_time: datetime, salt: str) -> GreenForecastDict:
        ts = sim_time.timestamp()
        if self._fn is None:
            return _mock_green_forecast(ts, self.dc_ids, random.Random(f"{self.seed}|{salt}"))
        result = self._fn(ts)
        missing = [d for d in self.dc_ids if d not in result]
        if missing:
            raise ValueError(f"predict_green_energy thiếu DC {missing}; nhận được {sorted(result)}")
        return {d: float(result[d]) for d in self.dc_ids}


# ==============================================================================
# 3. BACKEND ĐO MẠNG + TRUYỀN WORKLOAD
# ==============================================================================
@dataclass(frozen=True)
class ProbeContext:
    scenario: str
    size_mb: float
    iteration: int
    snapshot: int


@dataclass
class TransferOutcome:
    rtt_ms: float
    throughput_mbps: float
    migration_time_s: float
    packet_loss_rate: float


# Mock: đặc trưng "đường truy cập" từng DC theo từng kịch bản (rtt_ms, Mbps, loss %).
# Đường giữa a và b: rtt = rtt_a + rtt_b, thr = min(thr_a, thr_b), loss = loss_a + loss_b.
# Số lấy từ netem trong topology.yaml, chỉ để chạy thử khi chưa có hạ tầng.
MOCK_LEGS: Dict[str, Dict[str, tuple]] = {
    "KB1": {"dc1": (0.2, 1000, 0.0), "dc2": (0.2, 1000, 0.0), "dc3": (0.4, 1000, 0.0)},
    "KB2": {"dc1": (0.2, 1000, 0.0), "dc2": (20.0, 10, 0.0), "dc3": (10.0, 100, 0.0)},
    "KB3": {"dc1": (0.2, 1000, 0.0), "dc2": (150.0, 200, 0.5), "dc3": (20.0, 1000, 0.0)},
    "KB4": {"dc1": (0.2, 1000, 0.0), "dc2": (30.0, 50, 0.0), "dc3": (30.0, 50, 0.0)},
    "KB5": {"dc1": (0.2, 1000, 0.0), "dc2": (20.0, 60, 0.0), "dc3": (20.0, 60, 0.0)},
}


class MockBackend:
    """Số đo mạng giả có nhiễu ±noise (tái lập theo seed) để thử pipeline và hysteresis."""

    name = "mock"

    def __init__(self, st: Settings, seed: int) -> None:
        self.st = st
        self.seed = seed
        self.noise = float(st.pc.get("mock", {}).get("noise", 0.25))

    def prepare(self) -> None:
        pass

    def probe(self, current_dc: str, ctx: ProbeContext) -> Dict[str, NetworkMetrics]:
        legs = MOCK_LEGS.get(ctx.scenario, MOCK_LEGS["KB1"])
        rng = random.Random(f"{self.seed}|net|{ctx.scenario}|{ctx.size_mb}|{ctx.iteration}|{ctx.snapshot}|{current_dc}")
        out: Dict[str, NetworkMetrics] = {}
        for dc in self.st.dc_ids:
            if dc == current_dc:
                continue
            la, lb = legs.get(current_dc, (1.0, 1000, 0.0)), legs.get(dc, (1.0, 1000, 0.0))
            n = self.noise
            out[dc] = NetworkMetrics(
                target_dc=dc,
                rtt_ms=(la[0] + lb[0]) * (1 + rng.uniform(-n, n)),
                throughput_mbps=min(la[1], lb[1]) * (1 + rng.uniform(-n, n)),
                packet_loss_rate=max(0.0, (la[2] + lb[2]) * (1 + rng.uniform(-n, n))),
                queue_delay_ms=abs(rng.gauss(0.0, 1.0)),
            )
        return out

    def transfer(self, current_dc: str, target_dc: str, size_mb: float, m: NetworkMetrics, ctx: ProbeContext) -> TransferOutcome:
        rng = random.Random(f"{self.seed}|xfer|{ctx.scenario}|{ctx.iteration}|{ctx.snapshot}|{current_dc}|{target_dc}")
        thr = max(0.1, m.throughput_mbps * (1 + rng.uniform(-0.05, 0.05)))
        return TransferOutcome(
            rtt_ms=m.rtt_ms,
            throughput_mbps=thr,
            migration_time_s=size_mb * 8.0 / thr + 3 * m.rtt_ms / 1000.0,
            packet_loss_rate=m.packet_loss_rate,
        )

    def close(self) -> None:
        pass


_PING_LOSS = re.compile(r"([\d.]+)% packet loss")
_PING_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")

# Chạy trong container nguồn: dùng thẳng sender.send_workload và in 1 dòng JSON.
_SENDER_SNIPPET = (
    "import json,sys; sys.path.insert(0, sys.argv[1]); from sender import send_workload; "
    "r = send_workload(host=sys.argv[2], port=int(sys.argv[3]), workload_size_mb=float(sys.argv[4]), "
    "target_dc=sys.argv[5], timeout=float(sys.argv[6]), verify_checksum=sys.argv[7] == '1'); "
    "print('RESULT_JSON ' + json.dumps({'rtt_ms': r.rtt_ms, 'throughput_mbps': r.throughput_mbps, "
    "'migration_time_s': r.migration_time_s, 'status': r.status, 'checksum_valid': r.checksum_valid}))"
)


class DockerBackend:
    """Đo mạng (ping + iperf3) và truyền workload thật bằng `docker exec` vào dc1..dc3."""

    name = "docker"

    def __init__(self, st: Settings) -> None:
        self.st = st
        self.containers: Dict[str, str] = st.pc["docker"]["containers"]
        self.repo_mount: str = st.pc["docker"].get("repo_mount", "/app")
        self.probe_cfg: Dict[str, Any] = st.pc["probe"]
        self.xfer_cfg: Dict[str, Any] = st.pc["transfer"]

    # ---------- tiện ích docker ----------
    def _argv(self, dc: str, argv: Sequence[str], detach: bool = False) -> List[str]:
        return ["docker", "exec"] + (["-d"] if detach else []) + [self.containers[dc], *argv]

    def _exec(self, dc: str, argv: Sequence[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(self._argv(dc, argv), capture_output=True, text=True, timeout=timeout)

    def prepare(self) -> None:
        """Kiểm tra các container đang chạy trước khi bắt đầu."""
        for dc in self.st.dc_ids:
            p = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.containers[dc]],
                capture_output=True, text=True,
            )
            if p.returncode != 0 or p.stdout.strip() != "true":
                sys.exit(f"[LỖI] Container '{self.containers[dc]}' chưa chạy. Hãy chạy: docker compose up -d --build")

    # ---------- đo mạng ----------
    def _parse_ping(self, text: str) -> tuple:
        """Trả về (rtt_avg_ms, mdev_ms, loss_pct); không có phản hồi -> (unreachable, 0, 100)."""
        unreachable = float(self.probe_cfg["unreachable_rtt_ms"])
        loss_m, rtt_m = _PING_LOSS.search(text), _PING_RTT.search(text)
        loss = float(loss_m.group(1)) if loss_m else 100.0
        if not rtt_m:
            return unreachable, 0.0, loss
        return float(rtt_m.group(2)), float(rtt_m.group(4)), loss

    def _ping_argv(self, host: str, count: int) -> List[str]:
        return ["ping", "-c", str(count), "-i", str(self.probe_cfg["ping_interval_s"]), "-q", host]

    def _probe_one(self, src: str, dst: str) -> NetworkMetrics:
        pc, host = self.probe_cfg, self.st.ip_of(dst)
        port = str(pc["iperf_port"])

        # 1) RTT + mất gói khi mạng rảnh
        idle = self._exec(src, self._ping_argv(host, int(pc["ping_count"])), timeout=60)
        rtt_idle, _, loss = self._parse_ping(idle.stdout)

        # 2) throughput bằng iperf3, ping đồng thời để ước lượng queue delay
        self._exec(dst, ["pkill", "-f", f"iperf3 -s -p {port}"], timeout=10)
        time.sleep(0.3)
        self._exec(dst, ["iperf3", "-s", "-p", port, "-1", "-D"], timeout=10)
        time.sleep(0.5)

        iperf = subprocess.Popen(
            self._argv(src, ["iperf3", "-c", host, "-p", port, "-t", str(pc["iperf_seconds"]), "-J"]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.7)  # chờ iperf3 vào pha ổn định rồi mới ping
        loaded = subprocess.Popen(
            self._argv(src, self._ping_argv(host, int(pc["loaded_ping_count"]))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        iperf_out, _ = iperf.communicate(timeout=float(pc["iperf_seconds"]) + 30)
        loaded_out, _ = loaded.communicate(timeout=60)

        thr = float(pc["min_throughput_mbps"])
        try:
            end = json.loads(iperf_out)["end"]
            bps = (end.get("sum_received") or end.get("sum_sent"))["bits_per_second"]
            thr = max(thr, bps / 1e6)
        except (ValueError, KeyError, TypeError):
            logger.warning("iperf3 %s -> %s lỗi/không đọc được, dùng throughput tối thiểu.", src, dst)

        rtt_loaded, _, _ = self._parse_ping(loaded_out)
        queue_ms = max(0.0, rtt_loaded - rtt_idle) if rtt_loaded < float(pc["unreachable_rtt_ms"]) else 0.0
        return NetworkMetrics(target_dc=dst, rtt_ms=rtt_idle, throughput_mbps=thr,
                              packet_loss_rate=loss, queue_delay_ms=queue_ms)

    def probe(self, current_dc: str, ctx: ProbeContext) -> Dict[str, NetworkMetrics]:
        out: Dict[str, NetworkMetrics] = {}
        for dc in self.st.dc_ids:
            if dc != current_dc:
                out[dc] = self._probe_one(current_dc, dc)
                m = out[dc]
                logger.info("  đo %s->%s: RTT=%.2fms thr=%.1fMbps loss=%.2f%% queue=%.2fms",
                            current_dc, dc, m.rtt_ms, m.throughput_mbps, m.packet_loss_rate, m.queue_delay_ms)
        return out

    # ---------- truyền workload ----------
    def transfer(self, current_dc: str, target_dc: str, size_mb: float, m: NetworkMetrics, ctx: ProbeContext) -> TransferOutcome:
        port = int(self.xfer_cfg["port"])
        timeout = float(self.xfer_cfg["timeout_s"])
        rx_cmd = shlex.split(str(self.xfer_cfg["receiver_cmd"]).format(port=port))

        self._exec(target_dc, ["pkill", "-f", "receiver.py"], timeout=10)
        subprocess.run(self._argv(target_dc, rx_cmd, detach=True), check=True, capture_output=True)
        time.sleep(float(self.xfer_cfg.get("receiver_startup_s", 1.0)))
        try:
            p = subprocess.run(
                self._argv(current_dc, [
                    "python3", "-c", _SENDER_SNIPPET, f"{self.repo_mount}/src", self.st.ip_of(target_dc),
                    str(port), f"{size_mb:g}", target_dc, str(timeout),
                    "1" if self.xfer_cfg.get("verify_checksum", False) else "0",
                ]),
                capture_output=True, text=True, timeout=timeout + 30,
            )
        finally:
            self._exec(target_dc, ["pkill", "-f", "receiver.py"], timeout=10)

        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT_JSON ")), None)
        if p.returncode != 0 or line is None:
            tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
            raise RuntimeError(f"sender lỗi (rc={p.returncode}): {' | '.join(tail)}")
        r = json.loads(line[len("RESULT_JSON "):])
        if r.get("status", 0) != 0:
            raise RuntimeError(f"receiver báo status={r['status']} (checksum/thiếu dữ liệu)")
        return TransferOutcome(
            rtt_ms=float(r["rtt_ms"]),
            throughput_mbps=float(r["throughput_mbps"]),
            migration_time_s=float(r["migration_time_s"]),
            packet_loss_rate=m.packet_loss_rate,  # sender không đo loss -> lấy từ lần đo mạng
        )

    def close(self) -> None:
        for dc in self.st.dc_ids:
            self._exec(dc, ["pkill", "-f", "receiver.py"], timeout=10)
            self._exec(dc, ["pkill", "-f", "iperf3 -s -p " + str(self.probe_cfg["iperf_port"])], timeout=10)


# ==============================================================================
# 4. KỊCH BẢN (netem + lưu lượng nền)
# ==============================================================================
class ScenarioEnv:
    """Áp / gỡ netem và lưu lượng nền cho 1 kịch bản. Mock mode: không làm gì."""

    def __init__(self, st: Settings, scenario: str, active: bool) -> None:
        self.cfg = st.pc["scenarios"][scenario]
        self.scenario = scenario
        self.active = active
        self._bg_started = False

    def _run(self, cmd: str, **fmt: str) -> None:
        argv = shlex.split(cmd.format(scenario=self.scenario, **fmt))
        logger.info("  $ %s", " ".join(argv))
        subprocess.run(argv, cwd=REPO_ROOT, check=True)

    def __enter__(self) -> "ScenarioEnv":
        if not self.active:
            return self
        self._run(self.cfg.get("netem_cmd") or DEFAULT_NETEM_CMD)
        if self.cfg.get("background_start_cmd"):
            self._run(self.cfg["background_start_cmd"])
            self._bg_started = True
        elif "background_stop_cmd" in self.cfg:  # kịch bản KB5 mong đợi có lưu lượng nền
            logger.warning("%s: background_start_cmd chưa cấu hình -> KHÔNG có lưu lượng nền iperf3.", self.scenario)
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self.active:
            return
        cmds = []
        if self._bg_started:
            cmds.append(self.cfg["background_stop_cmd"])
        cmds.append(DEFAULT_NETEM_CMD.replace("{scenario}", "clear"))  # gỡ netem, trả mạng sạch
        for cmd in cmds:
            try:
                self._run(cmd)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.warning("Dọn dẹp kịch bản %s thất bại: %s", self.scenario, e)


# ==============================================================================
# 5. VÒNG LẶP THỰC NGHIỆM
# ==============================================================================
@dataclass
class StrategyState:
    name: str
    fn: Callable[..., DecisionResult]
    params: Optional[Dict[str, Any]]
    current_dc: str
    tracker: PingPongTracker
    migrations: int = 0
    failed: int = 0


class CsvLog:
    def __init__(self, path: Path, append: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not (append and path.is_file() and path.stat().st_size > 0)
        self._fh = path.open("a" if append else "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=CSV_HEADER)
        if new:
            self._w.writeheader()
        self.path = path

    def write(self, row: Dict[str, Any]) -> None:
        self._w.writerow({c: row.get(c, "") for c in CSV_HEADER})
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class Pipeline:
    def __init__(self, st: Settings, backend: Any, green: GreenSource, args: argparse.Namespace) -> None:
        self.st, self.backend, self.green, self.args = st, backend, green, args
        ex = st.pc["experiment"]
        self.iterations = args.iterations or int(ex["iterations"])
        self.snapshots = args.snapshots or int(ex["snapshots_per_iteration"])
        self.strategies = args.strategies or list(ex["strategies"])
        bad = [s for s in self.strategies if s not in DECIDERS]
        if bad:
            sys.exit(f"[LỖI] Chiến lược không hợp lệ: {bad}. Hợp lệ: {list(DECIDERS)}")
        self.seed = args.seed if args.seed is not None else int(ex["seed"])
        self.sim_start = datetime.fromisoformat(str(ex["sim_start"]))
        self.sim_step = timedelta(seconds=float(ex["sim_step_seconds"]))
        # hysteresis chỉ gắn cho Proposed; Baseline luôn nhận params=None
        self.proposed_params = {**(st.pc.get("decision_params") or {}),
                                "hysteresis_threshold": st.hysteresis_threshold}
        self.local = NetworkMetrics(target_dc="", **st.pc["probe"]["local"])

    # ---------- một lần lặp ----------
    def run_iteration(self, scenario: str, size_mb: float, it: int, log: CsvLog) -> Dict[str, StrategyState]:
        states = {
            s: StrategyState(s, DECIDERS[s], self.proposed_params if s == "Proposed" else None,
                             self.st.source_dc, PingPongTracker(self.st.source_dc))
            for s in self.strategies
        }
        for k in range(self.snapshots):
            ctx = ProbeContext(scenario, size_mb, it, k)
            sim_t = self.sim_start + k * self.sim_step
            green = self.green.forecast(sim_t, f"{scenario}|{size_mb}|{it}|{k}")
            logger.info("[%s it=%d snap=%d L=%gMB] xanh(kW): %s", scenario, it, k + 1, size_mb,
                        {d: round(v, 1) for d, v in green.items()})

            probes: Dict[str, Dict[str, NetworkMetrics]] = {}  # đo 1 lần cho mỗi vị trí hiện tại
            for s in states.values():
                if s.current_dc not in probes:
                    probes[s.current_dc] = self.backend.probe(s.current_dc, ctx)

            for s in states.values():
                self._step(s, scenario, size_mb, it, ctx, green, probes[s.current_dc], log)

            if self.backend.name != "mock" and k < self.snapshots - 1 and not self.args.no_sleep:
                time.sleep(self.st.interval_s)
        return states

    def _step(self, s: StrategyState, scenario: str, size_mb: float, it: int, ctx: ProbeContext,
              green: GreenForecastDict, probe: Dict[str, NetworkMetrics], log: CsvLog) -> None:
        here = s.current_dc
        local = NetworkMetrics(target_dc=here, rtt_ms=self.local.rtt_ms,
                               throughput_mbps=self.local.throughput_mbps,
                               packet_loss_rate=self.local.packet_loss_rate)
        metrics: NetworkMetricsDict = {**probe, here: local}
        result = s.fn(here, green, metrics, size_mb, s.params)

        decision, target = result.action, result.target_dc
        if decision == Action.MIGRATE:
            try:
                out = self.backend.transfer(here, target, size_mb, metrics[target], ctx)
                s.current_dc = target
                s.migrations += 1
            except Exception as exc:  # truyền lỗi: ở lại DC cũ, ghi nhận như STAY
                logger.error("  [%s] di trú %s->%s THẤT BẠI: %s", s.name, here, target, exc)
                s.failed += 1
                decision, target = Action.STAY, here
                out = TransferOutcome(local.rtt_ms, local.throughput_mbps, 0.0, local.packet_loss_rate)
        else:
            out = TransferOutcome(local.rtt_ms, local.throughput_mbps, 0.0, local.packet_loss_rate)

        pingpong = s.tracker.update(s.current_dc)
        log.write(create_csv_row(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            iteration=it, scenario=scenario, strategy=s.name, current_dc=here,
            decision=decision.value, target_dc=target, workload_size_mb=size_mb,
            rtt_ms=round(out.rtt_ms, 4), throughput_mbps=round(out.throughput_mbps, 4),
            migration_time_s=round(out.migration_time_s, 4),
            packet_loss_rate=round(out.packet_loss_rate, 4),
            green_energy_kw=round(green[target], 4), ping_pong_count=pingpong,
        ))
        logger.info("  [%-10s] %s: %s -> %s | %s", s.name, decision.value, here, target, result.reason)

    # ---------- một kịch bản ----------
    def run_scenario(self, scenario: str) -> None:
        sc_cfg = self.st.pc["scenarios"][scenario]
        sizes = [self.args.workload_size] if self.args.workload_size else \
            [float(x) for x in sc_cfg.get("workload_sizes_mb", [self.st.workload_size_mb])]
        suffix = "_mock" if self.backend.name == "mock" else ""
        out_dir = Path(self.args.out_dir) if self.args.out_dir else REPO_ROOT / self.st.pc["output"]["csv_dir"]
        log = CsvLog(out_dir / f"{scenario.lower()}{suffix}.csv", self.args.append)

        logger.info("=== %s: %s | sizes=%s MB | %d lần lặp x %d chu kỳ ===",
                    scenario, sc_cfg.get("description", ""), sizes, self.iterations, self.snapshots)
        totals = {s: {"rows": 0, "migrations": 0, "failed": 0, "pingpong": 0} for s in self.strategies}
        try:
            with ScenarioEnv(self.st, scenario, active=self.backend.name != "mock" and not self.args.no_netem):
                for size in sizes:
                    for it in range(1, self.iterations + 1):
                        for name, s in self.run_iteration(scenario, size, it, log).items():
                            t = totals[name]
                            t["rows"] += self.snapshots
                            t["migrations"] += s.migrations
                            t["failed"] += s.failed
                            t["pingpong"] += s.tracker.ping_pong_count
        finally:
            log.close()

        print(f"\n{scenario} -> {log.path}")
        print(f"  {'Chiến lược':<12}{'dòng':>6}{'di trú':>8}{'lỗi':>6}{'ping-pong':>11}")
        for name, t in totals.items():
            print(f"  {name:<12}{t['rows']:>6}{t['migrations']:>8}{t['failed']:>6}{t['pingpong']:>11}")


# ==============================================================================
# 6. MAIN
# ==============================================================================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Điều phối thực nghiệm di trú workload KB1..KB5.")
    ap.add_argument("--scenarios", nargs="+", default=["all"], help="KB1..KB5 hoặc 'all' (mặc định)")
    ap.add_argument("--mode", choices=["mock", "docker"], default="mock",
                    help="mock: số liệu giả, không cần Docker (mặc định); docker: đo/truyền thật")
    ap.add_argument("--iterations", type=int, default=None, help="Ghi đè experiment.iterations")
    ap.add_argument("--snapshots", type=int, default=None, help="Ghi đè experiment.snapshots_per_iteration")
    ap.add_argument("--strategies", nargs="+", default=None, help="Ghi đè danh sách chiến lược")
    ap.add_argument("--workload-size", type=float, default=None, help="Ép 1 kích thước workload (MB)")
    ap.add_argument("--out-dir", default=None, help="Thư mục CSV (mặc định theo pipeline_config.yaml)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--append", action="store_true", help="Ghi nối vào CSV có sẵn thay vì ghi đè")
    ap.add_argument("--no-netem", action="store_true", help="Docker: không áp/gỡ netem (tự quản lý)")
    ap.add_argument("--no-sleep", action="store_true", help="Docker: bỏ qua interval_seconds giữa các chu kỳ")
    ap.add_argument("--mock-green", action="store_true", help="Luôn dùng dự báo xanh giả, bỏ qua predict.py")
    ap.add_argument("-v", "--verbose", action="store_true", help="Log chi tiết từng quyết định")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logger.setLevel(logging.INFO if args.verbose else logging.WARNING)

    st = Settings.load()
    scenarios = SCENARIOS if "all" in [s.lower() for s in args.scenarios] else [s.upper() for s in args.scenarios]
    unknown = [s for s in scenarios if s not in st.pc["scenarios"]]
    if unknown:
        sys.exit(f"[LỖI] Kịch bản không có trong pipeline_config.yaml: {unknown}")

    seed = args.seed if args.seed is not None else int(st.pc["experiment"]["seed"])
    backend = MockBackend(st, seed) if args.mode == "mock" else DockerBackend(st)
    green = GreenSource(st.dc_ids, seed, force_mock=args.mock_green)
    if args.mode == "mock":
        print("[MOCK] Số đo mạng là GIẢ; CSV ghi ra kbN_mock.csv, không phải kết quả thực nghiệm.")
    if green.is_mock:
        print("[MOCK] Dự báo năng lượng xanh là GIẢ (chưa có predict_green_energy / model.pkl).")

    backend.prepare()
    pipe = Pipeline(st, backend, green, args)
    try:
        for sc in scenarios:
            pipe.run_scenario(sc)
    except KeyboardInterrupt:
        print("\nĐã dừng theo yêu cầu (Ctrl+C); đang dọn dẹp...")
    finally:
        backend.close()


if __name__ == "__main__":
    main()