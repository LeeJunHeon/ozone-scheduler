"""Application logging setup.

Logs are written under ./Logs as one file per day:
  - ozone_YYYY-MM-DD.log: normal app/controller/UI logs
  - crash_YYYY-MM-DD.log: uncaught exception and faulthandler output
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from datetime import datetime
from logging import Handler, LogRecord
from typing import Optional, TextIO


APP_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LOG_DIR = os.path.join(APP_ROOT, "Logs")


class DailyFileHandler(Handler):
    """Simple daily file handler that keeps the current file name date-stamped."""

    def __init__(self, prefix: str, encoding: str = "utf-8"):
        super().__init__()
        self.prefix = prefix
        self.encoding = encoding
        self._date = ""
        self._stream: Optional[TextIO] = None
        os.makedirs(LOG_DIR, exist_ok=True)

    def _path_for_today(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(LOG_DIR, f"{self.prefix}_{today}.log")

    def _ensure_stream(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._stream is not None and self._date == today:
            return
        if self._stream is not None:
            self._stream.close()
        self._date = today
        self._stream = open(self._path_for_today(), "a", encoding=self.encoding)

    def emit(self, record: LogRecord) -> None:
        try:
            self._ensure_stream()
            assert self._stream is not None
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


_crash_file: Optional[TextIO] = None
_original_excepthook = sys.excepthook


def _crash_path_for_today() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"crash_{today}.log")


def _get_crash_file() -> TextIO:
    global _crash_file
    path = _crash_path_for_today()
    if _crash_file is None or getattr(_crash_file, "name", None) != path:
        if _crash_file is not None:
            try:
                _crash_file.close()
            except Exception:
                pass
        _crash_file = open(path, "a", encoding="utf-8")
    return _crash_file


def _handle_exception(exc_type, exc_value, exc_tb) -> None:
    logging.getLogger(__name__).critical(
        "UNCAUGHT EXCEPTION", exc_info=(exc_type, exc_value, exc_tb)
    )
    try:
        import traceback

        f = _get_crash_file()
        f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] UNCAUGHT EXCEPTION\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        f.flush()
    except Exception:
        pass
    if _original_excepthook is not None:
        _original_excepthook(exc_type, exc_value, exc_tb)


def _handle_thread_exception(args: threading.ExceptHookArgs) -> None:
    _handle_exception(args.exc_type, args.exc_value, args.exc_traceback)


def setup_logging() -> None:
    """Configure app-wide file logging and crash logging."""
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = DailyFileHandler("ozone")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)
    root.addHandler(console)

    try:
        faulthandler.enable(_get_crash_file())
    except Exception:
        logging.getLogger(__name__).exception("faulthandler 활성화 실패")

    sys.excepthook = _handle_exception
    threading.excepthook = _handle_thread_exception
    logging.getLogger(__name__).info("logging initialized: %s", LOG_DIR)