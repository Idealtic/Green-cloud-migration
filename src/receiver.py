"""
src/receiver.py
---------------
TCP Socket Server mô phỏng Data Center đích nhận workload di trú.

Chức năng chính:
1. Lắng nghe kết nối TCP từ sender.py (Source DC).
2. Hỗ trợ bản tin RTT_PROBE / RTT_PONG để đo RTT tầng ứng dụng không cần ICMP.
3. Nhận WORKLOAD_START theo giao thức đóng khung (Header 82 bytes + Payload theo chunk).
4. Kiểm tra toàn vẹn dữ liệu bằng SHA-256 (tùy chọn), đo thời gian nhận và throughput thực tế.
5. Phản hồi bản tin WORKLOAD_ACK (32 bytes) chứa kết quả đo phía nhận về cho sender.

Phụ trách: Cao Văn Phú (feat/socket-engine)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ==============================================================================
# ĐẶC TẢ GIAO THỨC TRUYỀN WORKLOAD (APPLICATION FRAMING)
# ==============================================================================
BYTES_PER_MB = 1_000_000  # 1 MB = 10^6 bytes = 8 Mb (đồng bộ đơn vị Mbps)

MAGIC_WORKLOAD = b"GCMW"
MAGIC_ACK = b"GCMA"
MAGIC_PROBE = b"GCMP"

PROTO_VERSION = 1

MSG_WORKLOAD_START = 1
MSG_WORKLOAD_ACK = 2
MSG_RTT_PROBE = 3
MSG_RTT_PONG = 4

ACK_OK = 0
ACK_CHECKSUM_MISMATCH = 1
ACK_INCOMPLETE = 2

# Header chung 6 bytes đầu: magic (4s), version (B), msg_type (B)
PREFIX_STRUCT = struct.Struct("!4sBB")
# Phần còn lại của WORKLOAD_START (76 bytes): chunk_size (I), payload_size (Q), transfer_id (32s), sha256 (32s)
WORKLOAD_REST_STRUCT = struct.Struct("!IQ32s32s")
# Toàn bộ WORKLOAD_START = 82 bytes
WORKLOAD_HEADER_STRUCT = struct.Struct("!4sBBIQ32s32s")
# WORKLOAD_ACK = 32 bytes: magic (4s), status (B), checksum_verified (B), pad (2x), bytes_received (Q), rx_duration_s (d), rx_throughput_mbps (d)
WORKLOAD_ACK_STRUCT = struct.Struct("!4sBB2xQdd")
# Phần còn lại của RTT_PROBE / RTT_PONG (10 bytes): seq (H), client_ts_ns (Q) -> tổng 16 bytes
PROBE_REST_STRUCT = struct.Struct("!HQ")
PROBE_PACKET_STRUCT = struct.Struct("!4sBBHQ")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5001
DEFAULT_RECV_BUFFER = 65536


@dataclass
class ReceivedTransferRecord:
    """Thông tin một phiên nhận workload thành công tại Data Center đích."""

    transfer_id: str
    client_addr: Tuple[str, int]
    bytes_received: int
    chunk_size: int
    rx_duration_s: float
    rx_throughput_mbps: float
    checksum_verified: bool
    status: int
    saved_path: Optional[str] = None


def recv_exact(conn: socket.socket, num_bytes: int, allow_clean_eof: bool = False) -> bytes:
    """Đọc chính xác num_bytes từ TCP stream; nếu allow_clean_eof=True thì trả về b'' khi đóng sạch trước byte đầu."""
    if num_bytes == 0:
        return b""

    chunks = bytearray()
    remaining = num_bytes
    while remaining > 0:
        data = conn.recv(remaining)
        if not data:
            if allow_clean_eof and not chunks:
                return b""
            raise ConnectionError(
                f"Kết nối TCP bị đóng giữa chừng (còn thiếu {remaining}/{num_bytes} bytes)."
            )
        chunks.extend(data)
        remaining -= len(data)
    return bytes(chunks)


class WorkloadReceiver:
    """TCP Server nhận workload và trả lời đo kiểm RTT cho mô phỏng di trú liên DC."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        save_dir: Optional[str] = None,
        socket_timeout: float = 300.0,
    ) -> None:
        self.host = host
        self.port = port
        self.save_dir = Path(save_dir) if save_dir else None
        if self.save_dir is not None:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        self.socket_timeout = socket_timeout

        self._server_sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._threads: list[threading.Thread] = []
        self._records_lock = threading.Lock()
        self.records: list[ReceivedTransferRecord] = []

    @property
    def bound_port(self) -> int:
        """Trả về port thực tế đang lắng nghe (hữu ích khi khởi tạo với port=0)."""
        if self._server_sock is not None:
            return int(self._server_sock.getsockname()[1])
        return self.port

    def start(self) -> None:
        """Mở socket server và bind tới (host, port)."""
        if self._server_sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(16)
        sock.settimeout(0.5)
        self._server_sock = sock
        self._stop_event.clear()
        logger.info("Receiver đang lắng nghe tại %s:%d", self.host, self.bound_port)

    def start_in_background(self) -> int:
        """Khởi chạy accept loop trên luồng nền và trả về port đang lắng nghe."""
        self.start()
        self._thread = threading.Thread(
            target=self.serve_forever,
            name="WorkloadReceiverThread",
            daemon=True,
        )
        self._thread.start()
        return self.bound_port

    def serve_forever(self) -> None:
        """Vòng lặp chính chấp nhận kết nối từ các client."""
        self.start()
        assert self._server_sock is not None

        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            worker = threading.Thread(
                target=self._handle_client,
                args=(conn, addr),
                daemon=True,
            )
            worker.start()
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(worker)

    def stop(self) -> None:
        """Dừng server và đóng socket sạch sẽ."""
        self._stop_event.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        for worker in self._threads:
            if worker.is_alive():
                worker.join(timeout=1.0)
        logger.info("Đã dừng Receiver.")

    def __enter__(self) -> "WorkloadReceiver":
        self.start_in_background()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def _handle_client(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        """Xử lý từng kết nối TCP đến (có thể chứa nhiều bản tin RTT_PROBE hoặc 1 WORKLOAD_START)."""
        conn.settimeout(self.socket_timeout)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        with conn:
            while not self._stop_event.is_set():
                try:
                    prefix = recv_exact(conn, PREFIX_STRUCT.size, allow_clean_eof=True)
                except (ConnectionError, socket.timeout, OSError) as exc:
                    logger.debug("Kết nối %s kết thúc hoặc lỗi đọc prefix: %s", addr, exc)
                    break

                if not prefix:
                    break

                magic, version, msg_type = PREFIX_STRUCT.unpack(prefix)
                if version != PROTO_VERSION:
                    logger.warning("Phiên bản giao thức không hỗ trợ (%d) từ %s", version, addr)
                    break

                if magic == MAGIC_PROBE and msg_type == MSG_RTT_PROBE:
                    if not self._handle_rtt_probe(conn, addr):
                        break
                elif magic == MAGIC_WORKLOAD and msg_type == MSG_WORKLOAD_START:
                    self._handle_workload_transfer(conn, addr)
                    break
                else:
                    logger.warning("Nhận header không hợp lệ (%r, %d) từ %s", magic, msg_type, addr)
                    break

    def _handle_rtt_probe(self, conn: socket.socket, addr: Tuple[str, int]) -> bool:
        """Phản hồi ngay bản tin RTT_PONG để client tính thời gian khứ hồi."""
        try:
            rest = recv_exact(conn, PROBE_REST_STRUCT.size)
            if not rest:
                return False
            seq, client_ts_ns = PROBE_REST_STRUCT.unpack(rest)
            pong = PROBE_PACKET_STRUCT.pack(
                MAGIC_PROBE,
                PROTO_VERSION,
                MSG_RTT_PONG,
                seq,
                client_ts_ns,
            )
            conn.sendall(pong)
            return True
        except (ConnectionError, OSError) as exc:
            logger.debug("Lỗi xử lý RTT_PROBE từ %s: %s", addr, exc)
            return False

    def _handle_workload_transfer(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        """Nhận luồng chunk workload, xác thực SHA-256 và gửi bản tin WORKLOAD_ACK."""
        rest = recv_exact(conn, WORKLOAD_REST_STRUCT.size)
        if not rest:
            return

        chunk_size, payload_size, raw_id, expected_digest = WORKLOAD_REST_STRUCT.unpack(rest)
        transfer_id = raw_id.rstrip(b"\x00").decode("utf-8", errors="replace") or "unnamed"
        check_digest = expected_digest != (b"\x00" * 32)
        read_size = max(1024, min(chunk_size or DEFAULT_RECV_BUFFER, 4 * 1024 * 1024))

        out_file = None
        saved_path: Optional[str] = None
        if self.save_dir is not None:
            safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in transfer_id)
            target_path = self.save_dir / f"{safe_name}.bin"
            out_file = target_path.open("wb")
            saved_path = str(target_path)

        hasher = hashlib.sha256() if check_digest else None
        bytes_received = 0
        status = ACK_OK
        rx_start = time.perf_counter()

        try:
            remaining = payload_size
            while remaining > 0:
                chunk = conn.recv(min(read_size, remaining))
                if not chunk:
                    status = ACK_INCOMPLETE
                    break
                bytes_received += len(chunk)
                remaining -= len(chunk)
                if hasher is not None:
                    hasher.update(chunk)
                if out_file is not None:
                    out_file.write(chunk)
        finally:
            if out_file is not None:
                out_file.close()

        rx_duration_s = max(time.perf_counter() - rx_start, 1e-9)
        rx_throughput_mbps = (bytes_received * 8.0) / (rx_duration_s * BYTES_PER_MB)

        checksum_verified = False
        if status == ACK_OK and check_digest and hasher is not None:
            checksum_verified = hasher.digest() == expected_digest
            if not checksum_verified:
                status = ACK_CHECKSUM_MISMATCH
        elif status == ACK_OK and not check_digest:
            checksum_verified = True

        ack_packet = WORKLOAD_ACK_STRUCT.pack(
            MAGIC_ACK,
            status,
            1 if checksum_verified else 0,
            bytes_received,
            rx_duration_s,
            rx_throughput_mbps,
        )
        conn.sendall(ack_packet)

        record = ReceivedTransferRecord(
            transfer_id=transfer_id,
            client_addr=addr,
            bytes_received=bytes_received,
            chunk_size=chunk_size,
            rx_duration_s=rx_duration_s,
            rx_throughput_mbps=rx_throughput_mbps,
            checksum_verified=checksum_verified,
            status=status,
            saved_path=saved_path,
        )
        with self._records_lock:
            self.records.append(record)

        logger.info(
            "Đã nhận [%s] từ %s: %.2f MB trong %.4fs (%.2f Mbps) | status=%d checksum_ok=%s",
            transfer_id,
            addr[0],
            bytes_received / BYTES_PER_MB,
            rx_duration_s,
            rx_throughput_mbps,
            status,
            checksum_verified,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TCP Socket Server (Data Center đích) nhận workload di trú."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Địa chỉ bind server (mặc định: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Cổng lắng nghe (mặc định: 5001)")
    parser.add_argument(
        "--save-dir",
        default=None,
        help="Thư mục lưu file nhận được (bỏ trống để chỉ tính checksum và loại bỏ nhằm tiết kiệm đĩa).",
    )
    parser.add_argument("--verbose", action="store_true", help="Hiển thị log chi tiết")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    receiver = WorkloadReceiver(host=args.host, port=args.port, save_dir=args.save_dir)
    try:
        receiver.serve_forever()
    except KeyboardInterrupt:
        logger.info("Nhận tín hiệu ngắt từ bàn phím, đang tắt Receiver...")
    finally:
        receiver.stop()


if __name__ == "__main__":
    main()
