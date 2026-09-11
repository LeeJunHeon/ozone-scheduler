"""오존 스케줄러 진입점."""
import logging
import sys

from PyQt6.QtWidgets import QApplication

from core.app_logger import setup_logging
from core.config import AppConfig
from core.scheduler import OzoneController
from ui.main_window import MainWindow


logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    logger.info("Ozone Scheduler starting")

    app = QApplication(sys.argv)
    app.setApplicationName("Ozone Scheduler")

    config = AppConfig.load()
    controller = OzoneController(config)

    win = MainWindow(config, controller)
    win.show()

    controller.start()
    try:
        rc = app.exec()
        return rc
    finally:
        logger.info("Ozone Scheduler shutting down")
        controller.stop()
        config.save()
        logger.info("Ozone Scheduler stopped")


if __name__ == "__main__":
    sys.exit(main())