
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score
)

# 1. Duong dan du lieu
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "processed"

# 2. Doc du lieu Train va Test
train_data = pd.read_csv(DATA_DIR / "energy_3dc_train.csv")
test_data = pd.read_csv(DATA_DIR / "energy_3dc_test.csv")

print("=== DU LIEU ===")
print("Train:", train_data.shape)
print("Test:", test_data.shape)

# 3. Danh sach Data Center
DC_LIST = ["dc1", "dc2", "dc3"]

# 4. Persistence: du bao bang gia tri 1 gio truoc
print("\n=== PERSISTENCE BASELINE ===")

for dc in DC_LIST:
    # Gia tri thuc te can du bao
    y_true = test_data[dc]

    # Gia tri du bao = cong suat gio truoc
    y_pred = test_data[f"{dc}_lag1"]

    # 5. Tinh cac chi so danh gia
    mae = mean_absolute_error(y_true, y_pred)

    rmse = np.sqrt(
        mean_squared_error(y_true, y_pred)
    )

    r2 = r2_score(y_true, y_pred)

    print(f"\n{dc}:")
    print(f"  MAE  = {mae:.4f} kW")
    print(f"  RMSE = {rmse:.4f} kW")
    print(f"  R2   = {r2:.4f}")


# =========================================================
# 6. HUAN LUYEN RANDOM FOREST REGRESSION
# =========================================================

from sklearn.ensemble import RandomForestRegressor

# Danh sach dac trung dau vao
feature_cols = [
    "hour",
    "dc1_lag1", "dc1_lag2",
    "dc2_lag1", "dc2_lag2",
    "dc3_lag1", "dc3_lag2"
]

# Lay X, y tu hai tap da chia
X_train = train_data[feature_cols]
y_train = train_data[DC_LIST]

X_test = test_data[feature_cols]
y_test = test_data[DC_LIST]

# Khoi tao mo hinh
rf_model = RandomForestRegressor(
    n_estimators=200,
    max_depth=12,
    min_samples_leaf=3,
    random_state=42,
    n_jobs=-1
)

# Huan luyen tren tap Train
rf_model.fit(X_train, y_train)

# Du bao tren tap Test
rf_predictions = rf_model.predict(X_test)

print("\n=== RANDOM FOREST REGRESSION ===")

# Danh gia tung Data Center
for i, dc in enumerate(DC_LIST):

    y_true = y_test[dc]
    y_pred = rf_predictions[:, i]

    mae = mean_absolute_error(y_true, y_pred)

    rmse = np.sqrt(
        mean_squared_error(y_true, y_pred)
    )

    r2 = r2_score(y_true, y_pred)

    print(f"\n{dc}:")
    print(f"  MAE  = {mae:.4f} kW")
    print(f"  RMSE = {rmse:.4f} kW")
    print(f"  R2   = {r2:.4f}")


# =========================================================
# 7. SO SANH PERSISTENCE VA RANDOM FOREST
# =========================================================

comparison_results = []

for i, dc in enumerate(DC_LIST):

    y_true = y_test[dc]

    # Du bao Persistence
    persistence_pred = X_test[f"{dc}_lag1"]

    # Du bao Random Forest
    rf_pred = rf_predictions[:, i]

    # Tinh MAE
    mae_p = mean_absolute_error(y_true, persistence_pred)
    mae_rf = mean_absolute_error(y_true, rf_pred)

    # Tinh RMSE
    rmse_p = np.sqrt(
        mean_squared_error(y_true, persistence_pred)
    )
    rmse_rf = np.sqrt(
        mean_squared_error(y_true, rf_pred)
    )

    # Tinh R2
    r2_p = r2_score(y_true, persistence_pred)
    r2_rf = r2_score(y_true, rf_pred)

    # Phan tram cai thien MAE
    improvement = (
        (mae_p - mae_rf) / mae_p * 100
        if mae_p > 0 else np.nan
    )

    comparison_results.append({
        "DC": dc,
        "MAE_Persistence": mae_p,
        "MAE_RF": mae_rf,
        "RMSE_Persistence": rmse_p,
        "RMSE_RF": rmse_rf,
        "R2_Persistence": r2_p,
        "R2_RF": r2_rf,
        "MAE_Improvement_%": improvement
    })

# Tao bang ket qua
comparison_df = pd.DataFrame(comparison_results)

print("\n=== SO SANH HAI MO HINH ===")
print(comparison_df.round(4).to_string(index=False))

# Luu bang ket qua ML
RESULT_DIR = PROJECT_ROOT / "results" / "ml"
RESULT_DIR.mkdir(parents=True, exist_ok=True)

result_file = RESULT_DIR / "model_comparison.csv"
comparison_df.to_csv(result_file, index=False)

print("\nDa luu ket qua tai:", result_file)

# =========================================================
# 8. VE BIEU DO ACTUAL VS PREDICTED
# =========================================================

import matplotlib.pyplot as plt

# Tao thu muc luu bieu do
FIGURE_DIR = PROJECT_ROOT / "results" / "figures"
FIGURE_DIR.mkdir(parents=True, exist_ok=True)

# Lay thoi gian cua tap Test
test_timestamps = pd.to_datetime(test_data["timestamp"])

# Kiem tra kich thuoc ket qua du bao
assert rf_predictions.shape == (len(test_data), len(DC_LIST))

print("\n=== VE BIEU DO ACTUAL VS PREDICTED ===")

for i, dc in enumerate(DC_LIST):

    # Gia tri thuc te
    actual = y_test[dc].to_numpy()

    # Gia tri Random Forest du bao
    predicted = rf_predictions[:, i]

    # Tao bieu do
    plt.figure(figsize=(12, 5))

    plt.scatter(
        test_timestamps,
        actual,
        label="Actual",
        s=16,
        alpha=0.7
    )

    plt.scatter(
        test_timestamps,
        predicted,
        label="Random Forest",
        marker="x",
        s=20,
        alpha=0.7
    )

    # Tieu de va nhan
    plt.title(f"{dc.upper()} - Actual vs Predicted")
    plt.xlabel("Time")
    plt.ylabel("Simulated Green Power (kW)")

    plt.ylim(bottom=0)
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.gcf().autofmt_xdate()
    plt.tight_layout()

    # Luu anh
    output_path = (
        FIGURE_DIR / f"ml_actual_vs_predicted_{dc}.png"
    )

    plt.savefig(output_path, dpi=200)
    plt.close()

    print(f"Da luu: {output_path}")


# =========================================================
# 9. LUU MO HINH RANDOM FOREST
# =========================================================

import joblib

# Thu muc luu mo hinh
MODEL_DIR = PROJECT_ROOT / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / "model.pkl"

# Luu Random Forest da huan luyen
joblib.dump(rf_model, MODEL_PATH)

print("\n=== LUU RANDOM FOREST MODEL ===")
print("Duong dan:", MODEL_PATH)

# 10. Tai lai mo hinh de kiem tra
loaded_model = joblib.load(MODEL_PATH)

# Du bao lai bang mo hinh vua tai
loaded_predictions = loaded_model.predict(X_test)

# Kiem tra ket qua truoc va sau khi luu
assert np.allclose(
    rf_predictions,
    loaded_predictions
), "Ket qua du bao khong khop!"

# Kiem tra so bien dau vao va dau ra
assert loaded_model.n_features_in_ == len(feature_cols)
assert loaded_predictions.shape == (len(X_test), 3)

print("So feature:", loaded_model.n_features_in_)
print("Ten feature:", list(loaded_model.feature_names_in_))
print("Kich thuoc du bao:", loaded_predictions.shape)
print("Kiem tra luu va tai model: PASS")
