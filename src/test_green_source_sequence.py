
from pathlib import Path
from datetime import timezone

import pandas as pd

from pipeline import GreenSource

# 1. Doc du lieu Test
ROOT = Path(__file__).resolve().parents[1]

test_data = pd.read_csv(
    ROOT / "data/processed/energy_3dc_test.csv"
)

times = pd.to_datetime(
    test_data["timestamp"]
).sort_values().reset_index(drop=True)

# 2. Tim 4 timestamp lien tiep, cach nhau 1 gio
selected = None

for i in range(len(times) - 3):
    four_times = times.iloc[i:i + 4]

    gaps = four_times.diff().dropna()

    if (gaps == pd.Timedelta(hours=1)).all():
        selected = four_times
        break

if selected is None:
    raise ValueError("Khong tim duoc 4 gio lien tiep trong Test")

# 3. Khoi tao GreenSource tu pipeline
green = GreenSource(
    ["dc1", "dc2", "dc3"],
    seed=42
)

assert green.is_mock is False

print("\n=== KIEM THU 4 SNAPSHOT LIEN TIEP ===")
print("is_mock:", green.is_mock)

# 4. Du bao tai 4 thoi diem
for i, t in enumerate(selected, start=1):

    # Tam quy uoc thoi gian CSV la UTC
    sim_time = t.to_pydatetime().replace(
        tzinfo=timezone.utc
    )

    forecast = green.forecast(
        sim_time, f"snapshot-{i}"
    )

    assert set(forecast) == {"dc1", "dc2", "dc3"}
    assert all(0 <= v <= 100 for v in forecast.values())

    print(f"\nSnapshot {i}: {sim_time}")
    print({
        dc: round(value, 4)
        for dc, value in forecast.items()
    })

print("\nKIEM THU 4 SNAPSHOT: PASS")
