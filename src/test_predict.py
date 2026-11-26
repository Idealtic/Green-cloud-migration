
from datetime import datetime, timezone
import math

from predict import predict_green_energy

# 1. Kiem tra timestamp hop le
valid_ts = datetime(
    2025, 5, 6, 14,
    tzinfo=timezone.utc
).timestamp()

forecast = predict_green_energy(valid_ts)

assert set(forecast.keys()) == {"dc1", "dc2", "dc3"}

for dc, value in forecast.items():
    assert isinstance(value, (int, float))
    assert math.isfinite(value)
    assert 0 <= value <= 100

print("Test 1 - Timestamp hop le: PASS")


# 2. Kiem tra timestamp NaN
try:
    predict_green_energy(float("nan"))
except ValueError:
    print("Test 2 - Timestamp NaN: PASS")
else:
    raise AssertionError("Khong phat hien timestamp NaN")


# 3. Kiem tra timestamp ngoai dataset
invalid_ts = datetime(
    2026, 6, 21, 6,
    tzinfo=timezone.utc
).timestamp()

try:
    predict_green_energy(invalid_ts)
except ValueError:
    print("Test 3 - Timestamp ngoai dataset: PASS")
else:
    raise AssertionError("Khong phat hien timestamp ngoai dataset")

print("\nTAT CA KIEM THU: PASS")
