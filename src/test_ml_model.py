
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

# 1. Duong dan du an
PROJECT_ROOT = Path(__file__).resolve().parents[1]

MODEL_PATH = PROJECT_ROOT / "models" / "model.pkl"
TEST_PATH = (
    PROJECT_ROOT / "data" / "processed"
    / "energy_3dc_test.csv"
)

# 2. Tai mo hinh va du lieu Test
model = joblib.load(MODEL_PATH)
test_data = pd.read_csv(TEST_PATH)

# 3. Xac dinh dac trung va dau ra
FEATURE_COLS = [
    "hour",
    "dc1_lag1", "dc1_lag2",
    "dc2_lag1", "dc2_lag2",
    "dc3_lag1", "dc3_lag2"
]

DC_LIST = ["dc1", "dc2", "dc3"]

# 4. Kiem tra giao dien dau vao
assert list(model.feature_names_in_) == FEATURE_COLS

X_test = test_data[FEATURE_COLS]

assert not X_test.isna().any().any()

# 5. Du bao bang mo hinh da luu
predictions = model.predict(X_test)

# 6. Kiem tra ket qua
assert predictions.shape == (len(X_test), 3)
assert np.isfinite(predictions).all()
assert ((predictions >= 0) & (predictions <= 100)).all()

print("=== KIEM THU MODEL DOC LAP ===")
print("So mau Test:", len(X_test))
print("So feature:", len(FEATURE_COLS))
print("Kich thuoc dau ra:", predictions.shape)
print("Gia tri du bao hop le: PASS")

# 7. Hien thi 3 du bao dau tien
print("\n=== 3 DU BAO MAU ===")

for i in range(3):
    forecast = {
        dc: float(predictions[i, j])
        for j, dc in enumerate(DC_LIST)
    }

    print("\nTimestamp:", test_data.iloc[i]["timestamp"])
    print("Du bao:", forecast)

print("\nKIEM THU MODEL: PASS")
