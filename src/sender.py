"""
src/sender.py
-------------
TCP Socket Client mô phỏng Data Center nguồn truyền workload di trú và đo điều kiện mạng.

Chức năng chính:
1. Đo độ trễ khứ hồi (RTT) bằng TCP socket RTT_PROBE/RTT_PONG (tắt Nagle với TCP_NODELAY)
   hoặc tùy chọn dùng ICMP ping.
2. Truyền khối dữ liệu workload (theo kích thước MB cấu hình hoặc đọc từ tệp) qua TCP socket
   theo khung Header (82 bytes) + Chunk + ACK (32 bytes) để tránh tràn RAM với file 1 GB.
3. Tính toán thời gian hoàn thành di trú (migration_time_s) và thông lượng hiệu dụng (throughput_mbps).
4. Đóng gói kết quả đo thành NetworkMetrics (khớp src/interfaces.py) và xuất log CSV theo CSV_HEADER.

Phụ trách: Cao Văn Phú (feat/socket-engine)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import platform
import re
import socket
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from src.interfaces import CSV_HEADER, NetworkMetrics, create_csv_row
    from src.receiver import (
        ACK_OK,
        BYTES_PER_MB,
        DEFAULT_PORT,
        MAGIC_ACK,
        MAGIC_PROBE,
        MAGIC_WORKLOAD,
        MSG_RTT_PONG,
        MSG_RTT_PROBE,
        MSG_WORKLOAD_START,
        PROBE_PACKET_STRUCT,
        PROTO_VERSION,
        WORKLOAD_ACK_STRUCT,
        WORKLOAD_HEADER_STRUCT,
        recv_exact,
    )
except ImportError:
    from interfaces import CSV_HEADER, NetworkMetrics, create_csv_row
    from receiver import (
        ACK_OK,
        BYTES_PER_MB,
        DEFAULT_PORT,
        MAGIC_ACK,
        MAGIC_PROBE,
        MAGIC_WORKLOAD,
        MSG_RTT_PONG,
        MSG_RTT_PROBE,
        MSG_WORKLOAD_START,
        PROBE_PACKET_STRUCT,
        PROTO_VERSION,
        WORKLOAD_ACK_STRUCT,
        WORKLOAD_HEADER_STRUCT,
        recv_exact,
    )

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 65536


@dataclass
class TransferResult:
    """Kết quả chi tiết một lần truyền workload qua TCP socket."""

    target_dc: str
    host: str
    port: int
    transfer_id: str
    workload_size_mb: float
    bytes_sent: int
    chunk_size: int
    connect_time_ms: float
    rtt_ms: float
    migration_time_s: float
    throughput_mbps: float
    rx_duration_s: float
    rx_throughput_mbps: float
    checksum_valid: bool
    status: int
    timestamp: str


def measure_rtt_tcp(
    host: str,
    port: int = DEFAULT_PORT,
    count: int = 5,
    timeout: float = 5.0,
) -> Dict[str, float]:
    """
    Đo RTT mức ứng dụng qua TCP socket (gửi count bản tin RTT_PROBE và chờ RTT_PONG).

    Trả về dict gồm: connect_rtt_ms, min_rtt_ms, avg_rtt_ms, max_rtt_ms, jitter_ms.
    """
    if count <= 0:
        raise ValueError("count phải >= 1")

    samples_ms: list[float] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        t_conn_start = time.perf_counter()
        sock.connect((host, port))
        connect_rtt_ms = (time.perf_counter() - t_conn_start) * 1000.0

        for seq in range(1, count + 1):
            t_start_ns = time.perf_counter_ns()
            probe = PROBE_PACKET_STRUCT.pack(
                MAGIC_PROBE,
                PROTO_VERSION,
                MSG_RTT_PROBE,
                seq,
                t_start_ns,
            )
            sock.sendall(probe)
            pong_raw = recv_exact(sock, PROBE_PACKET_STRUCT.size)
            t_end_ns = time.perf_counter_ns()

            magic, version, msg_type, rx_seq, _ = PROBE_PACKET_STRUCT.unpack(pong_raw)
            if magic != MAGIC_PROBE or version != PROTO_VERSION or msg_type != MSG_RTT_PONG or rx_seq != seq:
                raise ValueError("Nhận phản hồi RTT_PONG không hợp lệ từ receiver.")

            samples_ms.append((t_end_ns - t_start_ns) / 1_000_000.0)

    jitter_ms = statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0
    return {
        "connect_rtt_ms": connect_rtt_ms,
        "min_rtt_ms": min(samples_ms),
        "avg_rtt_ms": statistics.mean(samples_ms),
        "max_rtt_ms": max(samples_ms),
        "jitter_ms": jitter_ms,
    }


def measure_ping(host: str, count: int = 4, timeout_s: int = 5) -> Dict[str, float]:
    """
    Wrapper chạy lệnh ping hệ thống (hỗ trợ Linux và Windows) để lấy avg_rtt_ms và packet_loss_rate (%).
    Nếu môi trường chặn ICMP hoặc lỗi lệnh sẽ ném RuntimeError.
    """
    is_windows = platform.system().lower().startswith("win")
    if is_windows:
        cmd = ["ping", "-n", str(count), "-w", str(timeout_s * 1000), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout_s), host]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = proc.stdout + "\n" + proc.stderr

    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:packet\s*)?loss", output, re.IGNORECASE)
    packet_loss_rate = float(loss_match.group(1)) if loss_match else (100.0 if proc.returncode != 0 else 0.0)

    # Linux: rtt min/avg/max/mdev = 10.123/12.456/15.789/1.234 ms
    linux_rtt = re.search(
        r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms",
        output,
    )
    if linux_rtt:
        return {
            "min_rtt_ms": float(linux_rtt.group(1)),
            "avg_rtt_ms": float(linux_rtt.group(2)),
            "max_rtt_ms": float(linux_rtt.group(3)),
            "jitter_ms": float(linux_rtt.group(4)),
            "packet_loss_rate": packet_loss_rate,
        }

    # Windows (English): Average = 12ms
    win_rtt = re.search(r"Average\s*=\s*(\d+(?:\.\d+)?)\s*ms", output, re.IGNORECASE)
    if win_rtt:
        avg_ms = float(win_rtt.group(1))
        return {
            "min_rtt_ms": avg_ms,
            "avg_rtt_ms": avg_ms,
            "max_rtt_ms": avg_ms,
            "jitter_ms": 0.0,
            "packet_loss_rate": packet_loss_rate,
        }

    if proc.returncode != 0:
        raise RuntimeError(f"Lệnh ping tới {host} thất bại: {output.strip()}")

    return {
        "min_rtt_ms": 0.0,
        "avg_rtt_ms": 0.0,
        "max_rtt_ms": 0.0,
        "jitter_ms": 0.0,
        "packet_loss_rate": packet_loss_rate,
    }


def _compute_payload_sha256(
    payload_size: int,
    pattern_chunk: bytes,
    file_path: Optional[Path] = None,
) -> bytes:
    """Tính SHA-256 theo từng chunk mà không nạp toàn bộ payload vào RAM."""
    hasher = hashlib.sha256()
    if file_path is not None:
        with file_path.open("rb") as fh:
            while True:
                buf = fh.read(len(pattern_chunk))
                if not buf:
                    break
                hasher.update(buf)
        return hasher.digest()

    remaining = payload_size
    chunk_len = len(pattern_chunk)
    while remaining > 0:
        if remaining >= chunk_len:
            hasher.update(pattern_chunk)
            remaining -= chunk_len
        else:
            hasher.update(pattern_chunk[:remaining])
            remaining = 0
    return hasher.digest()


def send_workload(
    host: str,
    port: int = DEFAULT_PORT,
    workload_size_mb: float = 100.0,
    file_path: Optional[str] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    target_dc: str = "dc2",
    transfer_id: Optional[str] = None,
    timeout: float = 300.0,
    verify_checksum: bool = True,
    measure_rtt_before_send: bool = True,
    rtt_probe_count: int = 3,
) -> TransferResult:
    """
    Truyền một workload từ Source DC sang Receiver DC qua TCP socket và đo hiệu năng.

    Tham số:
    - host, port: Địa chỉ IP/hostname và cổng của WorkloadReceiver.
    - workload_size_mb: Kích thước workload mô phỏng (MB, 1 MB = 10^6 bytes). Bỏ qua nếu truyền file_path.
    - file_path: Đường dẫn file thật cần truyền (tùy chọn).
    - chunk_size: Kích thước mỗi chunk gửi qua sendall() (mặc định 64 KB).
    - target_dc: Tên DC đích ("dc1", "dc2", "dc3").
    - verify_checksum: Nếu True, tính và đối chiếu SHA-256 ở receiver.
    - measure_rtt_before_send: Đo RTT TCP probe trước khi truyền để ghi nhận rtt_ms.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size phải > 0")

    src_path: Optional[Path] = Path(file_path) if file_path else None
    if src_path is not None:
        if not src_path.is_file():
            raise FileNotFoundError(f"Không tìm thấy tệp workload: {src_path}")
        payload_size = src_path.stat().st_size
        actual_size_mb = payload_size / BYTES_PER_MB
    else:
        if workload_size_mb < 0:
            raise ValueError("workload_size_mb không thể âm")
        payload_size = int(round(workload_size_mb * BYTES_PER_MB))
        actual_size_mb = workload_size_mb

    if transfer_id is None:
        transfer_id = f"{target_dc}_{int(time.time())}"

    # Mẫu chunk lặp cố định 64 KB (không cấp phát RAM cỡ GB)
    pattern_chunk = (b"GREEN_CLOUD_WORKLOAD_DATA_" * ((chunk_size // 26) + 1))[:chunk_size]
    digest = (
        _compute_payload_sha256(payload_size, pattern_chunk, src_path)
        if verify_checksum
        else (b"\x00" * 32)
    )

    measured_rtt_ms = 0.0
    if measure_rtt_before_send:
        rtt_stats = measure_rtt_tcp(host=host, port=port, count=rtt_probe_count, timeout=min(timeout, 10.0))
        measured_rtt_ms = rtt_stats["avg_rtt_ms"]

    transfer_id_bytes = transfer_id.encode("utf-8")[:32].ljust(32, b"\x00")
    header = WORKLOAD_HEADER_STRUCT.pack(
        MAGIC_WORKLOAD,
        PROTO_VERSION,
        MSG_WORKLOAD_START,
        chunk_size,
        payload_size,
        transfer_id_bytes,
        digest,
    )

    timestamp_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        t_total_start = time.perf_counter()
        sock.connect((host, port))
        t_after_connect = time.perf_counter()
        connect_time_ms = (t_after_connect - t_total_start) * 1000.0

        sock.sendall(header)

        if src_path is not None:
            with src_path.open("rb") as fh:
                while True:
                    buf = fh.read(chunk_size)
                    if not buf:
                        break
                    sock.sendall(buf)
        else:
            remaining = payload_size
            while remaining > 0:
                if remaining >= chunk_size:
                    sock.sendall(pattern_chunk)
                    remaining -= chunk_size
                else:
                    sock.sendall(pattern_chunk[:remaining])
                    remaining = 0

        ack_raw = recv_exact(sock, WORKLOAD_ACK_STRUCT.size)
        t_total_end = time.perf_counter()

    magic, status, checksum_flag, bytes_received, rx_duration_s, rx_throughput_mbps = (
        WORKLOAD_ACK_STRUCT.unpack(ack_raw)
    )
    if magic != MAGIC_ACK:
        raise ValueError(f"Gói ACK trả về sai magic bytes: {magic!r}")

    migration_time_s = max(t_total_end - t_total_start, 1e-9)
    throughput_mbps = (payload_size * 8.0) / (migration_time_s * BYTES_PER_MB)
    if not measure_rtt_before_send:
        measured_rtt_ms = connect_time_ms

    checksum_valid = bool(checksum_flag) and (bytes_received == payload_size) and (status == ACK_OK)
    if status != ACK_OK or not checksum_valid:
        logger.warning(
            "Truyền [%s] tới %s hoàn tất với cảnh báo: status=%d, rx=%d/%d, checksum_valid=%s",
            transfer_id,
            target_dc,
            status,
            bytes_received,
            payload_size,
            checksum_valid,
        )

    return TransferResult(
        target_dc=target_dc,
        host=host,
        port=port,
        transfer_id=transfer_id,
        workload_size_mb=actual_size_mb,
        bytes_sent=payload_size,
        chunk_size=chunk_size,
        connect_time_ms=connect_time_ms,
        rtt_ms=measured_rtt_ms,
        migration_time_s=migration_time_s,
        throughput_mbps=throughput_mbps,
        rx_duration_s=rx_duration_s,
        rx_throughput_mbps=rx_throughput_mbps,
        checksum_valid=checksum_valid,
        status=status,
        timestamp=timestamp_iso,
    )


def measure_network_metrics(
    host: str,
    port: int = DEFAULT_PORT,
    target_dc: str = "dc2",
    probe_size_mb: float = 1.0,
    rtt_count: int = 5,
    use_icmp_ping: bool = False,
) -> NetworkMetrics:
    """
    Đo điều kiện mạng tới một DC đích và trả về đối tượng NetworkMetrics theo chuẩn src/interfaces.py.
    """
    rtt_stats = measure_rtt_tcp(host=host, port=port, count=rtt_count)
    packet_loss_rate = 0.0
    if use_icmp_ping:
        try:
            ping_stats = measure_ping(host=host, count=rtt_count)
            packet_loss_rate = ping_stats["packet_loss_rate"]
        except RuntimeError as exc:
            logger.debug("Không chạy được ICMP ping tới %s (%s), giữ packet_loss_rate=0.0", host, exc)

    probe_transfer = send_workload(
        host=host,
        port=port,
        workload_size_mb=probe_size_mb,
        target_dc=target_dc,
        transfer_id=f"probe_{target_dc}",
        verify_checksum=False,
        measure_rtt_before_send=False,
    )

    return NetworkMetrics(
        target_dc=target_dc,
        rtt_ms=round(rtt_stats["avg_rtt_ms"], 4),
        throughput_mbps=round(probe_transfer.throughput_mbps, 4),
        packet_loss_rate=round(packet_loss_rate, 4),
        queue_delay_ms=round(rtt_stats["jitter_ms"], 4),
    )


def append_csv_log(csv_path: str, row: Dict[str, Any]) -> None:
    """Ghi một bản ghi kết quả vào file CSV theo đúng thứ tự cột CSV_HEADER."""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.is_file() and path.stat().st_size > 0

    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if not file_exists:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in CSV_HEADER})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TCP Socket Client (Source DC) truyền workload và đo điều kiện mạng."
    )
    parser.add_argument("--host", required=True, help="IP/hostname của Receiver")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Cổng Receiver (mặc định: 5001)")
    parser.add_argument("--size-mb", type=float, default=10.0, help="Kích thước workload giả lập (MB)")
    parser.add_argument("--file", default=None, help="Đường dẫn tệp workload thật (ghi đè --size-mb)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Kích thước chunk (bytes)")
    parser.add_argument("--target-dc", default="dc2", help="Tên DC đích (dc1/dc2/dc3)")
    parser.add_argument("--current-dc", default="dc1", help="Tên DC nguồn (dc1/dc2/dc3)")
    parser.add_argument("--scenario", default="KB1", help="Tên kịch bản khi ghi CSV")
    parser.add_argument("--strategy", default="Proposed", help="Tên chiến lược khi ghi CSV")
    parser.add_argument("--iteration", type=int, default=1, help="Lần lặp khi ghi CSV")
    parser.add_argument("--no-checksum", action="store_true", help="Tắt kiểm tra SHA-256")
    parser.add_argument("--log-csv", default=None, help="Đường dẫn file CSV lưu kết quả theo CSV_HEADER")
    parser.add_argument("--verbose", action="store_true", help="Hiển thị log chi tiết")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = send_workload(
        host=args.host,
        port=args.port,
        workload_size_mb=args.size_mb,
        file_path=args.file,
        chunk_size=args.chunk_size,
        target_dc=args.target_dc,
        verify_checksum=not args.no_checksum,
    )
    logger.info(
        "Kết quả truyền tới %s (%s:%d): %.2f MB | RTT=%.3f ms | Time=%.4f s | Throughput=%.2f Mbps | Checksum=%s",
        result.target_dc,
        result.host,
        result.port,
        result.workload_size_mb,
        result.rtt_ms,
        result.migration_time_s,
        result.throughput_mbps,
        result.checksum_valid,
    )

    if args.log_csv:
        row = create_csv_row(
            timestamp=result.timestamp,
            iteration=args.iteration,
            scenario=args.scenario,
            strategy=args.strategy,
            current_dc=args.current_dc,
            decision="MIGRATE",
            target_dc=args.target_dc,
            workload_size_mb=round(result.workload_size_mb, 4),
            rtt_ms=round(result.rtt_ms, 4),
            throughput_mbps=round(result.throughput_mbps, 4),
            migration_time_s=round(result.migration_time_s, 4),
            packet_loss_rate=0.0,
            green_energy_kw=0.0,
            ping_pong_count=0,
        )
        append_csv_log(args.log_csv, row)
        logger.info("Đã ghi log CSV vào %s", args.log_csv)


if __name__ == "__main__":
    main()
