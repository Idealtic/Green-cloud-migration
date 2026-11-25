
from pathlib import Path
from datetime import datetime, timezone
from functools import lru_cache
import math

import joblib
import numpy as np
import pandas as pd

try:
    from interfaces import GreenForecastDict
except ImportError:
    from src.interfaces import GreenForecastDict


# 1. Duong dan
PROJECT_ROOT = Path(__file__).resolve().parents[1]

MODEL_PATH = PROJECT_ROOT / "models" / "model.pkl"

TEST_DATA_PATH = (
    PROJECT_ROOT / "data" / "processed"
    / "energy_3dc_test.csv"
)

DC_LIST = ["dc1", "dc2", "dc3"]

FEATURE_COLS = [
    "hour",
    "dc1_lag1", "dc1_lag2",
    "dc2_lag1", "dc2_lag2",
    "dc3_lag1", "dc3_lag2"
]


# 2. Tai model va du lieu 1 lan
@lru_cache(maxsize=1)
def load_resources():
    model = joblib.load(MODEL_PATH)

    data = pd.read_csv(TEST_DATA_PATH)

    data["timestamp"] = pd.to_datetime(
        data["timestamp"],
        errors="raise"
    )

    if data["timestamp"].duplicated().any():
        raise ValueError("Timestamp trong Test bi trung lap")

    if list(model.feature_names_in_) != FEATURE_COLS:
        raise ValueError("Feature khong khop voi model")

    data = data.set_index("timestamp")

    return model, data


# 3. Ham du bao theo interface chung
def predict_green_energy(
    timestamp: float
) -> GreenForecastDict:

    if not math.isfinite(timestamp):
        raise ValueError("Timestamp khong hop le")

    # Tam quy uoc timestamp CSV theo UTC
    dt = datetime.fromtimestamp(
        timestamp, tz=timezone.utc
    ).replace(tzinfo=None)

    model, data = load_resources()

    if dt not in data.index:
        raise ValueError(
            f"Khong co du lieu lag tai {dt}. "
            "Chi ho tro timestamp da chuan bi trong Test."
        )

    # Chi lay features qua khu, khong lay target thuc te
    X = data.loc[[dt], FEATURE_COLS]

    prediction = model.predict(X)[0]

    if not np.isfinite(prediction).all():
        raise ValueError("Model tra ve NaN hoac Inf")

    if ((prediction < 0) | (prediction > 100)).any():
        raise ValueError("Cong suat du bao ngoai 0-100 kW")

    return {
        dc: float(prediction[i])
        for i, dc in enumerate(DC_LIST)
    }
