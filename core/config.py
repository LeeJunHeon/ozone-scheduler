"""설정값 dataclass + JSON 영속화."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from typing import List

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "settings.json")
)


@dataclass
class AppConfig:
    # 시리얼 (USB 릴레이)
    com_port: str = "COM5"
    baudrate: int = 9600

    # ALD TCP (AldModule.cs 서버)
    ald_host: str = "127.0.0.1"
    ald_port: int = 7000

    # 레시피 폴더 (NAS). 이 폴더의 *.csv 파일을 스캔해 이름 목록을 만듦.
    recipe_dir: str = r"\\VanaM_NAS\VanaM_toShare\JH_Lee\Recipe\ALD"

    # 자동 스케줄
    auto_enabled: bool = False
    on_time: str = "22:00"   # HH:MM
    off_time: str = "06:00"  # HH:MM
    # weekdays[0]=월 ... weekdays[6]=일
    weekdays: List[bool] = field(default_factory=lambda: [True] * 5 + [False] * 2)

    # 레시피 이름 (확장자 제외, 빈 문자열 = 사용 안 함)
    recipe_on: str = ""        # ① ON 직후
    recipe_pre_off: str = ""   # ② OFF 직전
    recipe_post_off: str = ""  # ③ OFF 직후

    # Google Chat 웹훅 URL (비어있으면 알림 안 보냄)
    chat_webhook_url: str = ""

    @classmethod
    def load(cls) -> "AppConfig":
        if not os.path.exists(CONFIG_PATH):
            return cls()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 누락 필드는 기본값으로 채움
            defaults = cls()
            for key in vars(defaults):
                if key not in data:
                    data[key] = getattr(defaults, key)
            return cls(**data)
        except Exception:
            logger.exception("설정 파일 로드 실패: %s", CONFIG_PATH)
            return cls()

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        except Exception:
            # 저장 실패해도 앱은 계속 동작
            logger.exception("설정 파일 저장 실패: %s", CONFIG_PATH)