
from pathlib import Path
import pandas as pd

# 1. Xác định đường dẫn thư mục gốc của dự án
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "raw"

# 2. Đọc thông tin các trạm điện
stations = pd.read_csv(DATA_DIR / "stations.csv")

# 3. Đọc dữ liệu năng lượng theo giờ
energy = pd.read_csv(
    DATA_DIR / "hourly_pv_weather_station.csv",
    usecols=[
        "station_hash_id",
        "measured_ts",
        "total_produced_energy"
    ]
)

# 4. Chọn 3 trạm đại diện cho 3 Data Center

# Mapping 3 tram dien thanh 3 Data Center gia lap
# dc1: DC nguon
# dc2: DC dich co nguon nang luong xanh thuan loi
# dc3: DC dich con lai

selected_stations = {
    "dc1": "FUSIONSOLAR Station 1",
    "dc2": "SOLIS Station 1",
    "dc3": "FUSIONSOLAR Station 2"
}


# 5. Hiển thị thông tin của từng trạm
for dc, station_name in selected_stations.items():
    station_row = stations.loc[stations["name"] == station_name]

    if len(station_row) != 1:
        raise ValueError(f"Khong xac dinh duoc tram: {station_name}")

    station_id = station_row.iloc[0]["station_hash_id"]
    station_data = energy[energy["station_hash_id"] == station_id]

    print(f"\n{dc}: {station_name}")
    print(f"  Station ID: {station_id}")
    print(f"  So ban ghi: {len(station_data)}")
    print(f"  Tu: {station_data['measured_ts'].min()}")
    print(f"  Den: {station_data['measured_ts'].max()}")


# 6. Tạo bảng ánh xạ mã trạm sang tên Data Center
station_id_to_dc = {}

for dc, station_name in selected_stations.items():
    station_id = stations.loc[
        stations["name"] == station_name,
        "station_hash_id"
    ].iloc[0]

    station_id_to_dc[station_id] = dc

# 7. Lọc dữ liệu của 3 trạm được chọn
filtered_energy = energy[
    energy["station_hash_id"].isin(station_id_to_dc)
].copy()

# 8. Gắn nhãn dc1, dc2, dc3 cho các bản ghi
filtered_energy["dc"] = (
    filtered_energy["station_hash_id"].map(station_id_to_dc)
)

# 9. Chuyển cột thời gian về định dạng datetime
filtered_energy["measured_ts"] = pd.to_datetime(
    filtered_energy["measured_ts"]
)

# 10. Kiểm tra kết quả
print("\n=== KET QUA LOC DU LIEU ===")
print("Tong so ban ghi:", len(filtered_energy))
print("\nSo ban ghi theo DC:")
print(filtered_energy["dc"].value_counts().sort_index())

print("\n5 dong du lieu dau tien:")
print(filtered_energy.head())


# 11. Kiểm tra trùng lặp theo DC và thời gian
duplicate_count = filtered_energy.duplicated(
    subset=["measured_ts", "dc"]
).sum()

print("\nSo ban ghi trung lap:", duplicate_count)

if duplicate_count > 0:
    raise ValueError(
        "Du lieu co timestamp trung lap trong cung mot DC!"
    )

# 12. Ghép dữ liệu 3 DC theo cùng timestamp
energy_3dc = filtered_energy.pivot(
    index="measured_ts",
    columns="dc",
    values="total_produced_energy"
)

# 13. Sắp xếp thời gian và các cột
energy_3dc = energy_3dc.sort_index()
energy_3dc = energy_3dc[["dc1", "dc2", "dc3"]]

# 14. Đổi measured_ts thành timestamp
energy_3dc = (
    energy_3dc
    .reset_index()
    .rename(columns={"measured_ts": "timestamp"})
)

energy_3dc.columns.name = None

# 15. Kiểm tra kết quả
print("\n=== BANG DU LIEU 3 DC ===")
print(energy_3dc.head(10))

print("\nSo timestamp khac nhau:", len(energy_3dc))

print("\nSo gia tri thieu theo tung DC:")
print(energy_3dc.isna().sum())

print("\nSo timestamp co du du lieu 3 DC:")
print(energy_3dc.dropna(subset=["dc1", "dc2", "dc3"]).shape[0])


# 16. Tạo thư mục lưu dữ liệu đã xử lý sơ bộ
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 17. Lưu bảng dữ liệu 3 DC
output_file = OUTPUT_DIR / "energy_3dc_pivot_raw.csv"

energy_3dc.to_csv(output_file, index=False)

print("\n=== LUU DU LIEU ===")
print("Da luu file:", output_file)

# 18. Đọc lại file để kiểm tra
check_data = pd.read_csv(output_file)

print("So dong:", len(check_data))
print("So cot:", len(check_data.columns))
print("Ten cot:", check_data.columns.tolist())

# 19. Kiểm tra số thời điểm có đủ cả 3 DC
complete_data = energy_3dc.dropna(
    subset=["dc1", "dc2", "dc3"]
).copy()

print("\nSo timestamp co du 3 DC:", len(complete_data))

# 20. Kiểm tra khoảng cách thời gian giữa
# các quan sát đầy đủ liên tiếp
time_gaps = complete_data["timestamp"].diff().dropna()

non_hourly_gaps = (time_gaps != pd.Timedelta(hours=1)).sum()

print("So khoang cach khong bang 1 gio:", non_hourly_gaps)


# 21. Doc thong tin cong suat dinh muc
devices = pd.read_csv(DATA_DIR / "devices.csv")

rated_power = (
    devices.groupby("station_hash_id")["max_power"]
    .sum(min_count=1)
)

print("\n=== KIEM TRA CONG SUAT DINH MUC ===")

for dc, station_name in selected_stations.items():
    station_id = stations.loc[
        stations["name"] == station_name,
        "station_hash_id"
    ].iloc[0]

    capacity = rated_power.loc[station_id]

    values = filtered_energy.loc[
        filtered_energy["dc"] == dc,
        "total_produced_energy"
    ]

    print(f"\n{dc}: {station_name}")
    print(f"  Tong max_power: {capacity}")
    print(f"  Gia tri nho nhat: {values.min()}")
    print(f"  Gia tri lon nhat: {values.max()}")
    print(f"  So gia tri am: {(values < 0).sum()}")
    print(f"  So gia tri vuot dinh muc: {(values > capacity).sum()}")



# =========================================================
# 22-24. KIEM TRA BAT THUONG TAI SOLIS STATION 1
# Doc lap voi cach gan ten dc1, dc2, dc3
# =========================================================

AUDIT_STATION = "SOLIS Station 1"

audit_dc = next(
    dc for dc, name in selected_stations.items()
    if name == AUDIT_STATION
)

audit_station_id = stations.loc[
    stations["name"] == AUDIT_STATION,
    "station_hash_id"
].iloc[0]

audit_capacity = rated_power.loc[audit_station_id]

audit_data = filtered_energy[
    filtered_energy["station_hash_id"] == audit_station_id
].copy()

audit_data = audit_data.sort_values("measured_ts")

# Kiem tra gia tri vuot nguong
audit_outliers = audit_data[
    audit_data["total_produced_energy"] > audit_capacity
].copy()

print("\n=== KIEM TRA BAT THUONG SOLIS ===")
print("Tram:", AUDIT_STATION)
print("Data Center hien tai:", audit_dc)
print("Cong suat dinh muc:", audit_capacity)
print("So gia tri vuot nguong:", len(audit_outliers))

print("\n10 gia tri lon nhat:")
print(
    audit_outliers.nlargest(
        10, "total_produced_energy"
    )[["measured_ts", "total_produced_energy"]]
    .to_string(index=False)
)

audit_outliers["month"] = (
    audit_outliers["measured_ts"].dt.to_period("M")
)

print("\nGia tri bat thuong theo thang:")
print(audit_outliers.groupby("month").size())

# Kiem tra thoi diem bat thuong
check_time = "2024-08-09 10:00:00"

sample = audit_data[
    audit_data["measured_ts"].between(
        "2024-08-09 08:00:00",
        "2024-08-09 12:00:00"
    )
]

print("\nDu lieu xung quanh thoi diem bat thuong:")
print(
    sample[["measured_ts", "total_produced_energy"]]
    .to_string(index=False)
)

# Doi chieu voi du lieu inverter
inverter = pd.read_csv(
    DATA_DIR / "energy_production_hourly_inverter.csv",
    usecols=[
        "device_hash_id",
        "measured_ts",
        "total_produced_energy",
        "ac_power"
    ]
)

audit_devices = devices.loc[
    devices["station_hash_id"] == audit_station_id,
    "device_hash_id"
]

inverter_sample = inverter[
    (inverter["device_hash_id"].isin(audit_devices))
    & (inverter["measured_ts"] == check_time)
].copy()

print("\n=== DOI CHIEU CAP INVERTER ===")
print("So inverter co ban ghi:", len(inverter_sample))
print(
    inverter_sample[
        ["device_hash_id", "total_produced_energy"]
    ].sort_values(
        "total_produced_energy", ascending=False
    ).to_string(index=False)
)

print(
    "Tong san luong inverter:",
    inverter_sample["total_produced_energy"].sum()
)

# Kiem tra inverter bat thuong da phat hien
abnormal_device_id = "2e1392d8173e175e4fa40a27aa555e09"

sample_detail = inverter[
    (inverter["device_hash_id"] == abnormal_device_id)
    & inverter["measured_ts"].between(
        "2024-08-09 08:00:00",
        "2024-08-09 12:00:00"
    )
]

print("\n=== DOI CHIEU ENERGY VA AC_POWER ===")
print(
    sample_detail[
        ["measured_ts", "total_produced_energy", "ac_power"]
    ].sort_values("measured_ts").to_string(index=False)
)

rated_device = devices.loc[
    devices["device_hash_id"] == abnormal_device_id,
    "max_power"
].iloc[0]

print("Cong suat dinh muc inverter:", rated_device)



# 25. Tao bang du lieu de xu ly bat thuong
energy_checked = energy_3dc.copy()

# 26. Lay cong suat dinh muc cua 3 DC
capacities = {}

for dc, station_name in selected_stations.items():
    station_id = stations.loc[
        stations["name"] == station_name,
        "station_hash_id"
    ].iloc[0]

    capacities[dc] = rated_power.loc[station_id]

# 27. Kiem tra va danh dau cac gia tri bat thuong
print("\n=== XU LY GIA TRI BAT THUONG ===")

for dc in ["dc1", "dc2", "dc3"]:
    capacity = capacities[dc]

    abnormal_mask = (
        (energy_checked[dc] < 0)
        | (energy_checked[dc] > capacity)
    )

    abnormal_count = abnormal_mask.sum()

    print(f"{dc}:")
    print(f"  Cong suat dinh muc: {capacity}")
    print(f"  So gia tri bat thuong: {abnormal_count}")

    # Chi thay doi tren ban sao, khong sua du lieu goc
    energy_checked.loc[abnormal_mask, dc] = float("nan")

# 28. Kiem tra ket qua sau khi danh dau
print("\nSo gia tri thieu sau khi xu ly:")
print(energy_checked.isna().sum())

print("\nSo timestamp co du 3 DC:")
print(
    energy_checked.dropna(
        subset=["dc1", "dc2", "dc3"]
    ).shape[0]
)


# 29. Kiem tra tinh lien tuc cua du lieu sau khi xu ly
complete_data = energy_checked.dropna(
    subset=["dc1", "dc2", "dc3"]
).copy()

complete_data = complete_data.sort_values("timestamp")

# Tinh khoang cach thoi gian giua hai ban ghi
complete_data["gap_hours"] = (
    complete_data["timestamp"].diff()
    .dt.total_seconds() / 3600
)

# Tao nhom moi khi hai ban ghi khong cach nhau 1 gio
complete_data["segment_id"] = (
    complete_data["gap_hours"] != 1
).cumsum()

# Thong ke cac doan thoi gian lien tuc
segments = complete_data.groupby("segment_id").agg(
    start=("timestamp", "min"),
    end=("timestamp", "max"),
    n_hours=("timestamp", "size")
)

segments = segments.sort_values(
    "n_hours", ascending=False
)

print("\n=== KIEM TRA TINH LIEN TUC ===")
print("So ban ghi day du:", len(complete_data))
print("So doan lien tuc:", len(segments))
print("Doan dai nhat (gio):", segments["n_hours"].max())

print("\n5 doan lien tuc dai nhat:")
print(segments.head(5))

print("\nSo ban ghi theo gio trong ngay:")
print(
    complete_data["timestamp"].dt.hour
    .value_counts()
    .sort_index()
)


# 30. Kiem tra so mau tiem nang cho du bao 1 gio truoc

usable_samples = complete_data[
    complete_data.groupby("segment_id").cumcount() >= 2
].copy()

print("\n=== KIEM TRA SO MAU CO THE DUNG CHO ML ===")
print("So timestamp day du:", len(complete_data))
print("So doan lien tuc:", len(segments))

print(
    "So mau co du 2 gia tri qua khu lien tiep:",
    len(usable_samples)
)

print(
    "So ngay co mau hop le:",
    usable_samples["timestamp"].dt.normalize().nunique()
)


# 31. Chuan hoa cong suat ve quy mo gia lap 100 kW

GREEN_REF_KW = 100.0

# Tao ban sao cua du lieu da kiem tra bat thuong
energy_scaled = energy_checked.copy()

print("\n=== CHUAN HOA CONG SUAT 3 DC ===")

for dc in ["dc1", "dc2", "dc3"]:
    rated_kw = float(capacities[dc])

    if rated_kw <= 0:
        raise ValueError(f"Cong suat dinh muc khong hop le: {dc}")

    # total_produced_energy: kWh trong 1 gio
    # E_hour / 1h -> cong suat trung binh kW
    # Chuan hoa theo cong suat dinh muc, sau do nhan 100 kW
    energy_scaled[dc] = (
        energy_checked[dc] / rated_kw * GREEN_REF_KW
    )

    valid_values = energy_scaled[dc].dropna()

    if ((valid_values < 0) | (valid_values > GREEN_REF_KW)).any():
        raise ValueError(f"Gia tri ngoai khoang 0-100 kW: {dc}")

    print(f"\n{dc}:")
    print("  Cong suat dinh muc:", rated_kw)
    print("  Gia tri nho nhat:", valid_values.min())
    print("  Gia tri lon nhat:", valid_values.max())

# 32. Kiem tra du lieu sau khi chuan hoa

print("\nSo gia tri thieu:")
print(energy_scaled.isna().sum())

print("\nSo timestamp co du 3 DC:")
print(
    energy_scaled.dropna(
        subset=["dc1", "dc2", "dc3"]
    ).shape[0]
)


# 33. Luu du lieu da kiem tra va chuan hoa

output_scaled = (
    OUTPUT_DIR / "energy_3dc_scaled_100kw.csv"
)

energy_scaled.to_csv(output_scaled, index=False)

print("\n=== LUU DATASET CHUAN HOA ===")
print("Duong dan:", output_scaled)

# Doc lai de xac nhan file
check_scaled = pd.read_csv(output_scaled)

print("So dong:", len(check_scaled))
print("So cot:", len(check_scaled.columns))
print("Ten cot:", check_scaled.columns.tolist())


# 34. Tao du lieu cho Machine Learning

# Chi giu cac timestamp co du du lieu 3 DC
ml_base = energy_scaled.dropna(
    subset=["dc1", "dc2", "dc3"]
).copy()

ml_base = ml_base.sort_values("timestamp")

# 35. Xac dinh cac doan lien tuc theo gio
ml_base["gap_hours"] = (
    ml_base["timestamp"].diff()
    .dt.total_seconds() / 3600
)

ml_base["segment_id"] = (
    ml_base["gap_hours"] != 1
).cumsum()

# 36. Tao cac dac trung lag 1 va lag 2
lag_columns = []

for dc in ["dc1", "dc2", "dc3"]:
    for lag in [1, 2]:
        col_name = f"{dc}_lag{lag}"

        ml_base[col_name] = (
            ml_base.groupby("segment_id")[dc]
            .shift(lag)
        )

        lag_columns.append(col_name)

# 37. Tao dac trung gio trong ngay
ml_base["hour"] = ml_base["timestamp"].dt.hour

# 38. Chi giu cac mau co du lag 1 va lag 2
ml_data = ml_base.dropna(
    subset=lag_columns
).copy()

# 39. Chon va sap xep cot
ml_columns = (
    ["timestamp", "hour"]
    + lag_columns
    + ["dc1", "dc2", "dc3"]
)

ml_data = ml_data[ml_columns]

# 40. Kiem tra ket qua
print("\n=== DU LIEU CHO RANDOM FOREST ===")
print("So mau hop le:", len(ml_data))

print("\nDanh sach cac cot:")
print(ml_data.columns.tolist())

print("\n5 dong dau tien:")
print(ml_data.head().to_string(index=False))


# 41. Luu dataset cho Machine Learning
ml_output = (
    OUTPUT_DIR / "energy_3dc_ml_features.csv"
)

ml_data.to_csv(ml_output, index=False)

print("\n=== LUU DU LIEU ML ===")
print("Duong dan:", ml_output)
print("So dong:", len(ml_data))
print("So cot:", len(ml_data.columns))


# 42. Kiem tra chat luong dataset cho ML

print("\n=== KIEM TRA CHAT LUONG DU LIEU ML ===")

# Kiem tra so mau va khoang thoi gian
print("So mau:", len(ml_data))
print("Tu ngay:", ml_data["timestamp"].min())
print("Den ngay:", ml_data["timestamp"].max())

# Kiem tra timestamp trung lap
duplicate_ts = ml_data["timestamp"].duplicated().sum()
print("\nSo timestamp trung lap:", duplicate_ts)

# Kiem tra gia tri thieu
print("\nSo gia tri thieu theo cot:")
print(ml_data.isna().sum())

# Kiem tra pham vi cong suat sau chuan hoa
print("\n=== THONG KE CONG SUAT 3 DC (kW) ===")

print(
    ml_data[["dc1", "dc2", "dc3"]]
    .describe()
    .round(3)
)

# Kiem tra gia tri nam ngoai khoang 0-100 kW
for dc in ["dc1", "dc2", "dc3"]:
    invalid = (
        (ml_data[dc] < 0) |
        (ml_data[dc] > 100)
    ).sum()

    print(f"{dc} - so gia tri ngoai [0,100]: {invalid}")

# Kiem tra so mau theo thang
print("\n=== SO MAU THEO THANG ===")

print(
    ml_data["timestamp"]
    .dt.to_period("M")
    .value_counts()
    .sort_index()
)

# Kiem tra ty le cong suat gan 0
print("\n=== TY LE CONG SUAT THAP ===")

for dc in ["dc1", "dc2", "dc3"]:
    low_count = (ml_data[dc] < 1.0).sum()
    low_percent = low_count / len(ml_data) * 100

    print(
        f"{dc}: {low_count} mau duoi 1 kW "
        f"({low_percent:.2f}%)"
    )


# 43. Chia Train/Test theo thoi gian

# Sap xep du lieu theo thoi gian tang dan
ml_data = ml_data.sort_values(
    "timestamp"
).reset_index(drop=True)

# Ty le Train/Test
TRAIN_RATIO = 0.8

n_samples = len(ml_data)
n_train = int(n_samples * TRAIN_RATIO)

# Chia theo vi tri, khong shuffle
train_data = ml_data.iloc[:n_train].copy()
test_data = ml_data.iloc[n_train:].copy()

# 44. Xac dinh feature va target
feature_cols = [
    "hour",
    "dc1_lag1", "dc1_lag2",
    "dc2_lag1", "dc2_lag2",
    "dc3_lag1", "dc3_lag2"
]

target_cols = ["dc1", "dc2", "dc3"]

X_train = train_data[feature_cols]
y_train = train_data[target_cols]

X_test = test_data[feature_cols]
y_test = test_data[target_cols]

# 45. Kiem tra ket qua
print("\n=== CHIA TRAIN/TEST ===")

print("Tong so mau:", n_samples)
print("So mau Train:", len(train_data))
print("So mau Test:", len(test_data))

print("\nKhoang thoi gian Train:")
print("Tu:", train_data["timestamp"].min())
print("Den:", train_data["timestamp"].max())

print("\nKhoang thoi gian Test:")
print("Tu:", test_data["timestamp"].min())
print("Den:", test_data["timestamp"].max())

print("\nKich thuoc cac tap:")
print("X_train:", X_train.shape)
print("y_train:", y_train.shape)
print("X_test:", X_test.shape)
print("y_test:", y_test.shape)

# 46. Dam bao khong trung lap thoi gian Train/Test
assert train_data["timestamp"].max() < test_data["timestamp"].min()

print("\nKiem tra thu tu thoi gian: PASS")


# 47. Luu rieng du lieu Train va Test

train_file = OUTPUT_DIR / "energy_3dc_train.csv"
test_file = OUTPUT_DIR / "energy_3dc_test.csv"

train_data.to_csv(train_file, index=False)
test_data.to_csv(test_file, index=False)

print("\n=== LUU TRAIN/TEST ===")
print("Train:", train_file)
print("Test:", test_file)

# 48. Doc lai de kiem tra
train_check = pd.read_csv(train_file)
test_check = pd.read_csv(test_file)

print("\nSo mau Train da luu:", len(train_check))
print("So mau Test da luu:", len(test_check))

print("So cot Train:", len(train_check.columns))
print("So cot Test:", len(test_check.columns))
