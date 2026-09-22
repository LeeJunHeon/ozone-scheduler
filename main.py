"""오존 스케줄러 진입점."""
import logging
import sys

from PyQt6.QtWidgets import QApplication

from core.app_logger import LOG_DIR, setup_logging
from core.config import AppConfig
from core.scheduler import OzoneController
from ui.main_window import MainWindow
from core.chat_notifier import ChatNotifier
from core.nas_log_sync import NasLogSyncWorker


logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    logger.info("Ozone Scheduler starting")

    app = QApplication(sys.argv)
    app.setApplicationName("Ozone Scheduler")

    config = AppConfig.load()

    chat_notifier = ChatNotifier(
        lambda: config.chat_webhook_url
    )
    chat_notifier.start()

    nas_sync = None
    if config.nas_log_enabled and config.nas_log_dir.strip():
        nas_sync = NasLogSyncWorker(
            local_dir=LOG_DIR,
            nas_root=config.nas_log_dir,
            interval_sec=config.nas_log_sync_interval_sec,
        )
        nas_sync.start()

    controller = OzoneController(
        config,
        chat_notifier=chat_notifier,
    )

    win = MainWindow(config, controller)
    win.show()

    controller.start()

    try:
        return app.exec()
    finally:
        logger.info("Ozone Scheduler shutting down")
        controller.stop()
        chat_notifier.stop()

        if nas_sync:
            nas_sync.stop()

        config.save()
        logger.info("Ozone Scheduler stopped")


if __name__ == "__main__":
    sys.exit(main())