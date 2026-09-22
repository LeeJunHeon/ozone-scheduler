from __future__ import annotations

import json
import logging
import queue
import threading
import time
from urllib import request
from urllib.error import URLError

logger = logging.getLogger(__name__)


class ChatNotifier:
    def __init__(self, url_provider):
        self._url_provider = url_provider
        self._queue: queue.Queue[str] = queue.Queue(maxsize=1000)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ChatNotifier",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, text: str) -> bool:
        try:
            self._queue.put_nowait(text)
            return True
        except queue.Full:
            logger.error("Google Chat 발송 큐 가득 참. 메시지 보관 실패")
            return False

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                text = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._send_with_retry(text)
            finally:
                self._queue.task_done()

    def _send_with_retry(self, text: str) -> None:
        retry_delays = (0, 5, 30)

        for attempt, delay in enumerate(retry_delays, start=1):
            if delay:
                if self._stop_event.wait(delay):
                    return

            try:
                self._send_once(text)
                logger.info(
                    "Google Chat 알림 전송 성공: attempt=%s",
                    attempt,
                )
                return
            except (URLError, OSError, ValueError) as e:
                logger.warning(
                    "Google Chat 알림 전송 실패: attempt=%s/%s, error=%s",
                    attempt,
                    len(retry_delays),
                    e,
                )

        logger.error("Google Chat 최종 전송 실패: %s", text)

    def _send_once(self, text: str) -> None:
        url = self._url_provider().strip()
        if not url:
            raise ValueError("Google Chat webhook URL이 비어 있음")

        data = json.dumps(
            {"text": text},
            ensure_ascii=False,
        ).encode("utf-8")

        req = request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )

        with request.urlopen(req, timeout=3.0) as response:
            if not 200 <= response.status < 300:
                raise OSError(f"HTTP status={response.status}")