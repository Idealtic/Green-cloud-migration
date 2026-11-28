import logging
from pathlib import Path
from argparse import Namespace

import pandas as pd

from pipeline import (
    Settings,
    MockBackend,
    GreenSource,
    Pipeline
)

# 1. Thu muc goc
ROOT = Path(__file__).resolve().parents[1]

# Hien thi ly do STAY/MIGRATE cua tung chien luoc
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)

logging.getLogger("pipeline").setLevel(logging.INFO)

# 2. Doc cau hinh cua nhom
st = Settings.load()

# 3. Chi thay doi thoi gian trong bo nho de test
# KHONG sua file pipeline_config.yaml cua Chi
st.pc["experiment"]["sim_start"] = (
    "2025-05-06T14:00:00+00:00"
)
st.pc["experiment"]["sim_step_seconds"] = 3600

# 4. Cau hinh test nho
OUTPUT_DIR = ROOT / "results" / "ml_integration_remapped"

args = Namespace(
    iterations=1,
    snapshots=4,
    strategies=[
        "Proposed",
        "Baseline_A",
        "Baseline_B"
    ],
    seed=42,
    workload_size=100.0,
    out_dir=str(OUTPUT_DIR),
    append=False,
    no_netem=True,
    no_sleep=True,
    mock_green=False
)

# 5. Khoi tao mang mock + ML that
backend = MockBackend(st, seed=42)

green = GreenSource(
    st.dc_ids,
    seed=42,
    force_mock=False
)

assert green.is_mock is False

print("\n=== KIEM TRA TICH HOP ===")
print("Du bao nang luong mock:", green.is_mock)
print("Mang su dung: MOCK")

# 6. Chay pipeline cho KB3
backend.prepare()

try:
    pipe = Pipeline(st, backend, green, args)
    pipe.run_scenario("KB3")
finally:
    backend.close()

# 7. Doc ket qua CSV
csv_path = OUTPUT_DIR / "kb3_mock.csv"

result = pd.read_csv(csv_path)

print("\n=== KET QUA TICH HOP ===")
print("So dong:", len(result))
print("Cac chien luoc:", result["strategy"].unique())

print("\nSo dong moi chien luoc:")
print(result["strategy"].value_counts())

# 8. Kiem tra ket qua
assert len(result) == 12

assert set(result["strategy"]) == {
    "Proposed",
    "Baseline_A",
    "Baseline_B"
}

assert result["green_energy_kw"].notna().all()

print("\nKIEM THU PIPELINE + ML: PASS")
