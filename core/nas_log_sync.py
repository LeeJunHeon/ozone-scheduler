from __future__ import annotations

import logging
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class NasLogSyncWorker:
    def __init__(
        self,
        local_dir: str,
        nas_root: str,
        interval_sec: int = 30,
    ):
        self.local_dir = Path(local_dir)
        self.nas_root = Path(nas_root)
        self.interval_sec = max(10, interval_sec)

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="NasLogSync",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

        # NAS가 멈춰 있어도 프로그램 종료를 오래 기다리지 않음
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sync_once()

                if self._last_error is not None:
                    logger.info("NAS 로그 동기화 연결 복구")
                    self._last_error = None

            except Exception as e:
                error_text = f"{type(e).__name__}: {e}"

                # 같은 오류를 30초마다 계속 기록하지 않도록 변경 시에만 기록
                if error_text != self._last_error:
                    logger.warning(
                        "NAS 로그 동기화 실패. 로컬 로그는 유지됨: %s",
                        error_text,
                    )
                    self._last_error = error_text

            self._stop_event.wait(self.interval_sec)

    def _sync_once(self) -> None:
        computer_name = socket.gethostname()
        destination_dir = self.nas_root / computer_name
        destination_dir.mkdir(parents=True, exist_ok=True)

        files = list(self.local_dir.glob("ozone_*.log"))
        files += list(self.local_dir.glob("crash_*.log"))

        for source in files:
            self._upload_snapshot(source, destination_dir)

    def _upload_snapshot(
        self,
        source: Path,
        destination_dir: Path,
    ) -> None:
        destination = destination_dir / source.name

        # 먼저 로컬 임시 스냅샷을 만든다.
        # 프로그램 로그 기록은 NAS 전송을 기다리지 않는다.
        temp_dir = Path(tempfile.gettempdir()) / "ozone_log_sync"
        temp_dir.mkdir(parents=True, exist_ok=True)

        local_snapshot = temp_dir / (
            f"{source.name}.{socket.gethostname()}.snapshot"
        )
        shutil.copy2(source, local_snapshot)

        # NAS에는 임시 이름으로 전송한 뒤 교체한다.
        nas_temp = destination.with_suffix(
            destination.suffix + ".uploading"
        )
        shutil.copy2(local_snapshot, nas_temp)
        os.replace(nas_temp, destination)

        try:
            local_snapshot.unlink()
        except OSError:
            pass