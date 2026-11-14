"""
src/analyze.py
--------------
Đọc 5 file CSV thực nghiệm (results/csv/kb1.csv -> kb5.csv), tính Mean/Std theo
(kịch bản x chiến lược), xuất bảng tổng hợp và vẽ biểu đồ so sánh 3 chiến lược.

Cách chạy (từ thư mục gốc của repo):
    python src/analyze.py                  # dùng dữ liệu thật trong results/csv/
    python src/analyze.py --demo           # dùng dữ liệu GIẢ để thử code (không đụng results/csv/)
    python src/analyze.py --csv-dir X --out-dir Y

Đầu ra:
    results/summary_stats.csv              # bảng Mean/Std
    results/figures/*.png                  # biểu đồ (chạy --demo thì ra results/figures_demo/)
"""

import argparse
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # không cần màn hình, chạy được trong mọi môi trường
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from interfaces import CSV_HEADER
except ImportError:
    from src.interfaces import CSV_HEADER

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ["KB1", "KB2", "KB3", "KB4", "KB5"]
STRATEGIES = ["Proposed", "Baseline_A", "Baseline_B"]
STRATEGY_COLORS = {"Proposed": "#2a7fbf", "Baseline_A": "#3a9d5d", "Baseline_B": "#d9822b"}
SCENARIO_LABELS = {
    "KB1": "KB1\nMạng lý tưởng",
    "KB2": "KB2\nBăng thông thấp",
    "KB3": "KB3\nRTT cao",
    "KB4": "KB4\nWorkload đổi",
    "KB5": "KB5\nNhiễu mạng nền",
}

# cột -> (nhãn trục, tên file). Mean/Std được tính cho đúng các cột này.
METRICS = {
    "migration_time_s": ("Thời gian di trú (s)", "migration_time"),
    "throughput_mbps": ("Thông lượng (Mbps)", "throughput"),
    "rtt_ms": ("RTT (ms)", "rtt"),
    "packet_loss_rate": ("Tỉ lệ mất gói (%)", "packet_loss"),
    "green_energy_kw": ("Công suất xanh tại DC đích (kW)", "green_energy"),
    "ping_pong_count": ("Số lần ping-pong", "ping_pong"),
}


# ------------------------------------------------------------------------------
# 1. ĐỌC VÀ KIỂM TRA DỮ LIỆU
# ------------------------------------------------------------------------------
def load_data(csv_dir: Path) -> pd.DataFrame:
    """Đọc kb1..kb5.csv; file nào thiếu thì cảnh báo và bỏ qua. Dừng nếu sai tên cột."""
    frames = []
    for sc in SCENARIOS:
        path = csv_dir / f"{sc.lower()}.csv"
        if not path.exists():
            print(f"[CẢNH BÁO] Chưa có {path.name}, bỏ qua {sc}.")
            continue
        df = pd.read_csv(path)
        missing = [c for c in CSV_HEADER if c not in df.columns]
        if missing:
            sys.exit(f"[LỖI] {path.name} thiếu cột: {missing}. Cần đúng CSV_HEADER trong interfaces.py.")
        frames.append(df[CSV_HEADER])

    if not frames:
        sys.exit(f"[LỖI] Không tìm thấy file kb*.csv nào trong {csv_dir}.")

    data = pd.concat(frames, ignore_index=True)

    unknown = sorted(set(data["strategy"]) - set(STRATEGIES))
    if unknown:
        print(f"[CẢNH BÁO] Tên chiến lược lạ (sẽ không vẽ): {unknown}. Mong đợi: {STRATEGIES}")

    numeric = list(METRICS) + ["workload_size_mb", "iteration"]
    for col in numeric:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    bad = data[numeric].isna().sum()
    bad = bad[bad > 0]
    if not bad.empty:
        print(f"[CẢNH BÁO] Có ô rỗng/không phải số (sẽ bị bỏ khỏi Mean/Std):\n{bad.to_string()}")
    return data


# ------------------------------------------------------------------------------
# 2. TÍNH MEAN / STD
# ------------------------------------------------------------------------------
def summarize(data: pd.DataFrame) -> pd.DataFrame:
    """Mean, Std (độ lệch chuẩn mẫu, ddof=1) và số lần lặp n theo (scenario, strategy)."""
    grouped = data.groupby(["scenario", "strategy"])
    out = grouped.size().rename("n").to_frame()
    for col in METRICS:
        out[f"{col}_mean"] = grouped[col].mean()
        out[f"{col}_std"] = grouped[col].std(ddof=1)  # n=1 -> NaN, được xử lý lúc vẽ
    out["migrate_rate"] = grouped["decision"].apply(lambda s: (s == "MIGRATE").mean())
    out = out.reset_index()

    order_s = {s: i for i, s in enumerate(SCENARIOS)}
    order_t = {s: i for i, s in enumerate(STRATEGIES)}
    out = out.sort_values(
        ["scenario", "strategy"],
        key=lambda col: col.map(order_s if col.name == "scenario" else order_t).fillna(99),
    )
    return out.reset_index(drop=True)


# ------------------------------------------------------------------------------
# 3. VẼ BIỂU ĐỒ
# ------------------------------------------------------------------------------
def plot_metric(summary: pd.DataFrame, col: str, ylabel: str, out_path: Path) -> None:
    """Cột nhóm: trục x = kịch bản, mỗi nhóm 3 cột = 3 chiến lược, thanh sai số = Std."""
    scenarios = [s for s in SCENARIOS if s in set(summary["scenario"])]
    strategies = [s for s in STRATEGIES if s in set(summary["strategy"])]
    width = 0.8 / max(len(strategies), 1)
    x = np.arange(len(scenarios))

    fig, ax = plt.subplots(figsize=(9, 4.8))
    for i, st in enumerate(strategies):
        sub = summary[summary["strategy"] == st].set_index("scenario")
        means = [sub[f"{col}_mean"].get(sc, np.nan) for sc in scenarios]
        stds = [sub[f"{col}_std"].get(sc, np.nan) for sc in scenarios]
        ax.bar(
            x + (i - (len(strategies) - 1) / 2) * width,
            means,
            width,
            yerr=np.nan_to_num(stds),
            capsize=3,
            label=st,
            color=STRATEGY_COLORS[st],
            edgecolor="white",
            linewidth=0.6,
        )

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS[s] for s in scenarios])
    ax.set_ylabel(ylabel)
    ax.set_title(f"{ylabel}: so sánh 3 chiến lược (Mean ± Std)")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(title="Chiến lược", frameon=False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


# ------------------------------------------------------------------------------
# 4. DỮ LIỆU GIẢ ĐỂ THỬ CODE (--demo)
# ------------------------------------------------------------------------------
def make_demo_csvs(csv_dir: Path, seed: int = 0) -> None:
    """Sinh kb1..kb5.csv giả đúng CSV_HEADER. CHỈ để thử code, không phải số liệu thật."""
    rng = np.random.default_rng(seed)
    # (rtt, throughput) cơ sở theo kịch bản, chỉ để biểu đồ có hình dạng
    base = {"KB1": (2, 900), "KB2": (20, 40), "KB3": (120, 300), "KB4": (15, 200), "KB5": (30, 120)}
    mig_prob = {"Proposed": 0.4, "Baseline_A": 0.85, "Baseline_B": 0.15}
    pingpong = {"Proposed": 0.3, "Baseline_A": 2.5, "Baseline_B": 1.5}
    for sc, (rtt0, thr0) in base.items():
        rows = []
        for st in STRATEGIES:
            for it in range(1, 6):
                size = float(rng.choice([200, 500, 1000]))
                thr = max(5.0, rng.normal(thr0, thr0 * 0.08))
                rows.append(
                    {
                        "timestamp": f"2026-11-20T10:{it:02d}:00",
                        "iteration": it,
                        "scenario": sc,
                        "strategy": st,
                        "current_dc": "dc1",
                        "decision": "MIGRATE" if rng.random() < mig_prob[st] else "STAY",
                        "target_dc": "dc2",
                        "workload_size_mb": size,
                        "rtt_ms": max(0.1, rng.normal(rtt0, rtt0 * 0.1)),
                        "throughput_mbps": thr,
                        "migration_time_s": size * 8 / thr * (1.2 if st == "Baseline_A" else 1.0),
                        "packet_loss_rate": abs(rng.normal(0.5, 0.3)),
                        "green_energy_kw": rng.normal(85 if st != "Baseline_B" else 40, 5),
                        "ping_pong_count": int(rng.poisson(pingpong[st])),
                    }
                )
        pd.DataFrame(rows, columns=CSV_HEADER).to_csv(csv_dir / f"{sc.lower()}.csv", index=False)


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Phân tích 5 CSV thực nghiệm và vẽ biểu đồ.")
    ap.add_argument("--csv-dir", type=Path, default=REPO_ROOT / "results" / "csv")
    ap.add_argument("--out-dir", type=Path, default=None, help="Thư mục xuất biểu đồ")
    ap.add_argument("--demo", action="store_true", help="Dùng dữ liệu giả để thử code")
    args = ap.parse_args()

    tmp = None
    if args.demo:
        tmp = tempfile.TemporaryDirectory()
        csv_dir = Path(tmp.name)
        make_demo_csvs(csv_dir)
        out_dir = args.out_dir or REPO_ROOT / "results" / "figures_demo"
        summary_path = out_dir / "summary_stats_demo.csv"
        print("[DEMO] Đang dùng dữ liệu GIẢ, không phải kết quả thực nghiệm.")
    else:
        csv_dir = args.csv_dir
        out_dir = args.out_dir or REPO_ROOT / "results" / "figures"
        summary_path = REPO_ROOT / "results" / "summary_stats.csv"

    data = load_data(csv_dir)
    summary = summarize(data)

    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(summary_path, index=False, float_format="%.4f")
    print(f"Đã ghi bảng tổng hợp: {summary_path}")

    for col, (ylabel, fname) in METRICS.items():
        plot_metric(summary, col, ylabel, out_dir / f"{fname}.png")
    print(f"Đã vẽ {len(METRICS)} biểu đồ vào: {out_dir}")

    if tmp is not None:
        tmp.cleanup()


if __name__ == "__main__":
    main()