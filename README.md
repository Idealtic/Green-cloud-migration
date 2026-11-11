# Green Cloud Migration

Nghiên cứu ứng dụng Machine Learning dự đoán năng lượng tái tạo kết hợp đánh giá điều kiện mạng hỗ trợ di trú workload giữa các Cloud Data Center.

## Cấu trúc thư mục

```text
green-cloud-migration/
├── configs/              # Cấu hình topology và pipeline
├── scripts/              # Script tc netem và background traffic
├── src/                  # Mã nguồn chính (socket, ML, thuật toán, pipeline)
├── models/               # Lưu trữ file trọng số mô hình ML (.pkl)
├── data/                 # Dữ liệu chuỗi thời gian năng lượng
└── results/              # Kết quả đo đạc (CSV, PCAP, biểu đồ)
```

## Yêu cầu môi trường
- Python 3.9+
- Linux (Ubuntu 20.04/22.04 khuyến nghị để hỗ trợ đầy đủ `tc netem`)
- Docker & Docker Compose
