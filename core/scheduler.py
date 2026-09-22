"""오존 스케줄러 컨트롤러.

워커 스레드에서 매 1초 tick:
  1. IDLE이고 자동 모드면 시각/요일 일치 시 ON/OFF 시퀀스 시작
  2. OFF 시퀀스 진행 중이면 단계별 폴링/전이

UI 스레드에서 호출 가능한 메서드:
  - manual_on(), manual_off(): 수동 트리거
  - update_relay_dependencies(): 설정 변경 시 호출 (필요하면)

UI로 신호:
  - log(level, text): 로그
  - state_changed(state_name): 시퀀스 상태
  - relay_state_changed(is_on): 릴레이 ON/OFF 변동
"""
from __future__ import annotations

import json
import os
import logging
import threading
import time
from datetime import datetime, timedelta
from enum import Enum
from urllib import request as _urlrequest
from urllib.error import URLError

from PyQt6.QtCore import QObject, pyqtSignal

from core.ald_client import AldClient, AldClientError
from core.config import AppConfig
from core.relay_client import RelayClient, RelayError


class SeqState(Enum):
    IDLE = "idle"
    ON_RUNNING = "on_running"
    ON_RECIPE_RUNNING = "on_recipe_running"
    OFF_WAIT_IDLE = "off_wait_idle"
    OFF_PRE_RUNNING = "off_pre_running"
    OFF_POST_RUNNING = "off_post_running"


# 정책 상수
RECIPE_POLL_INTERVAL_SEC = 1
RECIPE_START_CONFIRM_TIMEOUT_SEC = 60
RECIPE_POLL_TIMEOUT_SEC = 60 * 60 * 6
RELAY_OFF_RETRY_COUNT = 3
RELAY_OFF_RETRY_DELAY_SEC = 1.0
OFF_IDLE_WAIT_TIMEOUT_SEC = 5 * 60
OFF_IDLE_POLL_INTERVAL_SEC = 2


logger = logging.getLogger(__name__)


class OzoneController(QObject):
    log = pyqtSignal(str, str)              # (level: 'info'|'warn'|'error', text)
    state_changed = pyqtSignal(str)         # SeqState.value
    relay_state_changed = pyqtSignal(bool)  # True=ON, False=OFF

    def __init__(self, config: AppConfig, chat_notifier):
        super().__init__()
        self.config = config
        self._chat_notifier = chat_notifier

        self._state = SeqState.IDLE
        self.relay_on = False

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

        # 같은 분에 두 번 트리거되지 않도록
        self._last_trigger_key: tuple = ()

        # 시간 경과 계산에는 시스템 시각 변경의 영향을 받지 않는 값을 사용
        self._recipe_polling_started: float = 0.0
        self._next_recipe_poll_at: float = 0.0
        self._recipe_seen_running: bool = False

        # 자동 트리거 여부 (로그 태그용)
        self._current_is_auto: bool = False

    # ============ 스레드 제어 ============

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception("scheduler tick 예외")
                self._emit_log("error", f"tick 예외: {e}")
                self._recover_from_unexpected_exception(e)
            time.sleep(1.0)

    def _recover_from_unexpected_exception(self, exc: Exception) -> None:
        """예상 밖 예외로 시퀀스가 중간 상태에 갇히지 않도록 복구한다."""
        try:
            st = self._get_state()
        except Exception:
            st = SeqState.IDLE

        if st == SeqState.IDLE:
            return

        self._emit_log(
            "error",
            f"예상 밖 예외로 {st.value} 상태에서 복구 진입: {exc}",
        )

        try:
            # 오존 안전 우선:
            # 릴레이가 켜졌을 가능성이 있는 시퀀스에서는 OFF를 한 번 더 보낸다.
            if st in {
                SeqState.ON_RUNNING,
                SeqState.ON_RECIPE_RUNNING,
                SeqState.OFF_WAIT_IDLE,
                SeqState.OFF_PRE_RUNNING,
                SeqState.OFF_POST_RUNNING,
            } or self.relay_on:
                self._send_relay_off_with_retry("[비상 OFF]", f"예외 복구: {st.value}")
        except Exception:
            logger.exception("예외 복구 중 릴레이 OFF 실패")
        finally:
            self._set_state(SeqState.IDLE)

    # ============ 클라이언트 팩토리 (매번 생성, 짧은 연결 정책) ============

    def _ald(self, timeout: float = 3.0) -> AldClient:
        return AldClient(self.config.ald_host, self.config.ald_port, timeout=timeout)

    def _relay(self) -> RelayClient:
        return RelayClient(self.config.com_port, self.config.baudrate)

    def _resolve_recipe_path(self, name: str) -> str:
        """레시피 이름을 recipe_dir 기준 풀 경로로 변환.

        - name이 이미 .csv로 끝나면 그대로
        - 아니면 .csv 확장자 추가
        - 빈 문자열이면 빈 문자열 반환
        """
        if not name:
            return ""
        rdir = self.config.recipe_dir or ""
        if name.lower().endswith(".csv"):
            return os.path.join(rdir, name)
        return os.path.join(rdir, name + ".csv")

    def _notify_chat(self, text: str) -> None:
        url = (self.config.chat_webhook_url or "").strip()
        if not url:
            return

        if not self._chat_notifier.enqueue(text):
            self._emit_log(
                "error",
                "Google Chat 발송 큐 저장 실패",
            )

    def _emit_log(self, level: str, text: str) -> None:
        log_level = {
            "info": logging.INFO,
            "warn": logging.WARNING,
            "error": logging.ERROR,
        }.get(level, logging.INFO)

        # 로컬 파일 로그에 먼저 기록
        logger.log(log_level, text)

        # Qt signal을 통해 UI 최근 동작 창에 표시
        self.log.emit(level, text)

    @staticmethod
    def _ald_status_reason(status: dict) -> str:
        state = status.get("state")

        if status.get("alarm"):
            reason = "ALD Alarm 발생"
        else:
            reason = {
                "idle": "ALD 대기 상태",
                "running": "ALD 공정 또는 수동 명령 실행 중",
                "preheating": "ALD 예열 또는 시작 확인 중",
                "error": "ALD 오류 상태",
            }.get(state, "ALD 상태 확인 불가")

        detail = str(status.get("message") or "").strip()
        if detail:
            return f"{reason}\n상세: {detail}"

        return reason

    def _notify_schedule_skipped(
        self, tag: str, action: str, reason: str
    ) -> None:
        text = (
            f"{tag} 예약 동작을 시작하지 않았습니다.\n"
            f"사유: {reason}\n"
            f"이번 예약에서는 릴레이 {action} 명령을 전송하지 않았습니다."
        )
        self._emit_log("warn", text)
        self._notify_chat(text)

    # ============ 상태 전이 ============

    def _set_state(self, s: SeqState) -> None:
        with self._lock:
            self._state = s
        self.state_changed.emit(s.value)

    def _get_state(self) -> SeqState:
        with self._lock:
            return self._state

    # ============ tick 디스패처 ============

    def _tick(self) -> None:
        st = self._get_state()

        if st == SeqState.IDLE:
            self._check_schedule()

        elif st == SeqState.OFF_WAIT_IDLE:
            self._tick_off_wait_idle()

        elif st == SeqState.ON_RECIPE_RUNNING:
            self._tick_on_recipe_running()

        elif st == SeqState.OFF_PRE_RUNNING:
            self._tick_off_pre_running()

        elif st == SeqState.OFF_POST_RUNNING:
            self._tick_off_post_running()

    def _tick_off_wait_idle(self) -> None:
        now = time.monotonic()

        if now < self._next_off_status_poll_at:
            return

        self._next_off_status_poll_at = (
            now + OFF_IDLE_POLL_INTERVAL_SEC
        )
        elapsed = now - self._off_wait_started

        try:
            status = self._ald().get_status()

        except AldClientError as e:
            self._emit_log(
                "warn",
                f"[자동 OFF] ALD 상태 조회 실패: {e}",
            )

            if elapsed >= OFF_IDLE_WAIT_TIMEOUT_SEC:
                self._force_relay_off(
                    "[자동 OFF]",
                    "ALD 상태를 5분 동안 확인하지 못함",
                )
            return

        state = status["state"]

        # ALD alarm 또는 error 상태
        if status["alarm"] or state == "error":
            self._force_relay_off(
                "[자동 OFF]",
                self._ald_status_reason(status),
            )
            return

        # 정상적으로 idle 복귀
        if state == "idle":
            self._emit_log(
                "info",
                "[자동 OFF] ALD idle 확인 → 자동 OFF 진행",
            )
            self._do_pre_off_recipe("[자동 OFF]")
            return

        # running 또는 preheating 상태가 5분 이상 지속됨
        if elapsed >= OFF_IDLE_WAIT_TIMEOUT_SEC:
            self._force_relay_off(
                "[자동 OFF]",
                f"ALD {state} 상태가 5분 이상 지속됨",
            )
            return

    # ============ 스케줄 체크 ============

    def _active_weekday(self, dt_date) -> bool:
        """해당 날짜가 자동 ON 기준 활성 요일인지 확인."""
        if len(self.config.weekdays) < 7:
            return False
        return bool(self.config.weekdays[dt_date.weekday()])


    def _check_schedule(self) -> None:
        if not self.config.auto_enabled:
            return

        # ON/OFF 시간이 같으면 자동 실행하지 않는다.
        if self.config.on_time == self.config.off_time:
            return

        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.date()

        # ON은 해당 날짜의 요일 설정을 따른다.
        if (
            hhmm == self.config.on_time
            and self._active_weekday(today)
            and self._last_trigger_key != (today.isoformat(), "on")
        ):
            self._last_trigger_key = (today.isoformat(), "on")
            self._current_is_auto = True
            self._start_on_sequence()
            return

        # OFF는 on_time 이후 같은 날에 끄는 경우와,
        # 자정을 넘어 다음날 끄는 경우를 구분한다.
        # 예: on=07:00, off=00:00이면 토요일 00:00 OFF는 금요일 ON에 속해야 한다.
        off_schedule_date = today
        if self.config.off_time <= self.config.on_time:
            off_schedule_date = today - timedelta(days=1)

        if (
            hhmm == self.config.off_time
            and self._active_weekday(off_schedule_date)
            and self._last_trigger_key != (off_schedule_date.isoformat(), "off")
        ):
            self._last_trigger_key = (off_schedule_date.isoformat(), "off")
            self._current_is_auto = True
            self._start_off_sequence()

    # ============ 수동 트리거 (UI 스레드에서 호출) ============

    def manual_on(self) -> None:
        with self._lock:
            if self._state != SeqState.IDLE:
                self._emit_log(
                    "warn",
                    "다른 시퀀스 진행 중. 수동 ON 거부",
                )
                return

            self._state = SeqState.ON_RUNNING

        self.state_changed.emit(SeqState.ON_RUNNING.value)
        self._current_is_auto = False
        self._emit_log("info", "[수동 ON] 요청 접수")

        threading.Thread(
            target=self._manual_on_direct,
            name="ManualRelayOn",
            daemon=True,
        ).start()

    def manual_off(self) -> None:
        with self._lock:
            if self._state != SeqState.IDLE:
                self._emit_log(
                    "warn",
                    "다른 시퀀스 진행 중. 수동 OFF 거부",
                )
                return

            self._state = SeqState.ON_RUNNING

        self.state_changed.emit(SeqState.ON_RUNNING.value)
        self._current_is_auto = False
        self._emit_log("info", "[수동 OFF] 요청 접수")

        threading.Thread(
            target=self._manual_off_direct,
            name="ManualRelayOff",
            daemon=True,
        ).start()

    # ============ ON 시퀀스 (단일 단계, 동기) ============

    def _manual_on_direct(self) -> None:
        """수동 ON: ALD 확인/레시피 없이 릴레이 ON 명령만 보낸다."""
        tag = "[수동 ON]"
        self._set_state(SeqState.ON_RUNNING)
        try:
            self._relay().on()
            self.relay_on = True
            self.relay_state_changed.emit(True)
            self._emit_log("info", f"{tag} 릴레이 ON OK")
        except RelayError as e:
            self._emit_log("error", f"{tag} 릴레이 ON 실패: {e}")
        finally:
            self._set_state(SeqState.IDLE)


    def _manual_off_direct(self) -> None:
        """수동 OFF: ALD 확인/레시피 없이 릴레이 OFF 명령만 보낸다."""
        tag = "[수동 OFF]"
        self._set_state(SeqState.ON_RUNNING)
        try:
            self._relay().off()
            self.relay_on = False
            self.relay_state_changed.emit(False)
            self._emit_log("info", f"{tag} 릴레이 OFF OK")
        except RelayError as e:
            self._emit_log("error", f"{tag} 릴레이 OFF 실패: {e}")
        finally:
            self._set_state(SeqState.IDLE)

    def _start_on_sequence(self) -> None:
        tag = "[자동 ON]"
        self._current_is_auto = True
        self._set_state(SeqState.ON_RUNNING)

        # 자동 ON 시작 전 ALD 상태 확인
        try:
            status = self._ald().get_status()
        except AldClientError as e:
            self._notify_schedule_skipped(
                tag, "ON", f"ALD 상태 조회 실패\n상세: {e}"
            )
            self._set_state(SeqState.IDLE)
            return

        if status["state"] != "idle" or status["alarm"]:
            self._notify_schedule_skipped(
                tag, "ON", self._ald_status_reason(status)
            )
            self._set_state(SeqState.IDLE)
            return

        # 릴레이 ON
        try:
            self._relay().on()
            self.relay_on = True
            self.relay_state_changed.emit(True)
            self._emit_log("info", f"{tag} 릴레이 ON OK")
        except RelayError as e:
            self._emit_log("error", f"{tag} 릴레이 ON 실패: {e}")
            self._notify_chat(f"{tag} 실패: 릴레이 ON 실패 ({e})")
            self._set_state(SeqState.IDLE)
            return

        # ① ON 직후 레시피가 없으면 여기서 자동 ON 완료
        if not self.config.recipe_on:
            self._emit_log("info", f"{tag} ① 레시피 사용 안 함")
            self._notify_chat(
                f"{tag} 릴레이 ON 명령 전송 완료. ① 레시피 사용 안 함"
            )
            self._set_state(SeqState.IDLE)
            return

        # ① ON 직후 레시피 시작
        csv_path = self._resolve_recipe_path(self.config.recipe_on)
        try:
            result = self._ald().start_recipe(csv_path)
        except AldClientError as e:
            self._emit_log("error", f"{tag} ① START_ALD 실패: {e}")
            self._notify_chat(f"{tag} 실패: ① START_ALD 실패 ({e})")
            self._safe_relay_off_after_on_failure(tag)
            self._set_state(SeqState.IDLE)
            return

        if result.get("result") != "success":
            msg = result.get("message")
            self._emit_log("error", f"{tag} ① 레시피 시작 실패: {msg}")
            self._notify_chat(f"{tag} 실패: ① 레시피 시작 실패 ({msg})")
            self._safe_relay_off_after_on_failure(tag)
            self._set_state(SeqState.IDLE)
            return

        self._emit_log("info", f"{tag} ① 레시피 시작 OK ({self.config.recipe_on}) → 완료 대기")
        self._notify_chat(f"{tag} ① 레시피 시작: {self.config.recipe_on}")

        self._begin_recipe_wait(SeqState.ON_RECIPE_RUNNING)

    def _tick_on_recipe_running(self) -> None:
        self._tick_recipe_wait("[자동 ON]", "①")

    # ============ OFF 시퀀스 (3단계, 비동기) ============

    def _start_off_sequence(self) -> None:
        self._current_is_auto = True
        self._off_wait_started = time.monotonic()
        self._next_off_status_poll_at = 0.0
        self._set_state(SeqState.OFF_WAIT_IDLE)

        self._emit_log(
            "info",
            "[자동 OFF] ALD idle 대기 시작",
        )

        # 다음 scheduler tick에서 상태를 확인하게 둔다.
        # 여기서 직접 _tick_off_wait_idle()을 호출하지 않아도 됨.

    def _send_relay_off_with_retry(
        self,
        tag: str,
        reason: str = "",
        retry_count: int | None = None,
    ) -> bool:
        """릴레이 OFF 명령을 보낸다. retry_count=1이면 재시도 없이 1회만 시도한다."""
        attempts = retry_count if retry_count is not None else RELAY_OFF_RETRY_COUNT
        attempts = max(1, attempts)

        suffix = f" ({reason})" if reason else ""
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                self._relay().off()
                self.relay_on = False
                self.relay_state_changed.emit(False)
                self._emit_log(
                    "info", f"{tag} 릴레이 OFF 명령 전송 완료{suffix}"
                )
                return True
            except RelayError as e:
                last_error = e
                level = "error" if attempt == attempts else "warn"
                self._emit_log(
                    level,
                    f"{tag} 릴레이 OFF 실패 {attempt}/{attempts}{suffix}: {e}",
                )
                if attempt < attempts:
                    time.sleep(RELAY_OFF_RETRY_DELAY_SEC)

        self._notify_chat(
            f"{tag} 릴레이 OFF 명령 전송을 확인하지 못했습니다{suffix}.\n"
            f"상세: {last_error}\n"
            "실제 릴레이 상태는 확인되지 않았습니다."
        )
        return False


    def _safe_relay_off_after_on_failure(self, tag: str) -> None:
        """ON 후 ① 레시피 시작 실패 시 오존 릴레이가 켜진 채 남지 않도록 OFF 시도."""
        if self.relay_on:
            self._emit_log("warn", f"{tag} ① 레시피 시작 실패 → 안전상 릴레이 OFF 시도")
        else:
            self._emit_log(
                "warn",
                f"{tag} ① 레시피 시작 실패 → relay_on 추정값은 OFF, 그래도 OFF 명령 시도",
            )

        self._send_relay_off_with_retry(
            tag,
            "① 레시피 시작 실패",
            retry_count=self._relay_off_attempt_count_for_current_mode(),
        )


    def _relay_off_attempt_count_for_current_mode(self) -> int:
        """자동 시퀀스는 재시도, 수동 시퀀스는 1회만 시도한다."""
        return RELAY_OFF_RETRY_COUNT if self._current_is_auto else 1


    def _force_relay_off(self, tag: str, reason: str) -> None:
        """기존 오류 시 OFF 정책을 수행하고, 후속 레시피는 시작하지 않는다."""
        text = (
            f"{tag} 후속 레시피를 실행하지 않고 릴레이 OFF를 시도합니다.\n"
            f"사유: {reason}"
        )
        self._emit_log("warn", text)
        self._notify_chat(text)

        sent = self._send_relay_off_with_retry(
            tag,
            reason,
            retry_count=self._relay_off_attempt_count_for_current_mode(),
        )

        if sent:
            self._notify_chat(
                f"{tag} 릴레이 OFF 명령 전송 완료.\n"
                "③ 직후 레시피는 실행하지 않았습니다."
            )

        # 실패 알림은 _send_relay_off_with_retry()에서 전송합니다.
        self._set_state(SeqState.IDLE)

    def _do_pre_off_recipe(self, tag: str) -> None:
        """자동 OFF의 ② OFF 직전 레시피 실행. 없으면 바로 릴레이 OFF."""

        if not self.config.recipe_pre_off:
            self._emit_log("info", f"{tag} ② 직전 레시피 사용 안 함 → 릴레이 OFF 진행")
            self._do_relay_off(tag)
            return

        try:
            csv_path = self._resolve_recipe_path(self.config.recipe_pre_off)
            result = self._ald().start_recipe(csv_path)
        except AldClientError as e:
            self._force_relay_off(
                tag,
                f"② 시작 요청 결과를 확인하지 못했습니다: {e}",
            )
            return

        if result.get("result") != "success":
            self._force_relay_off(
                tag,
                f"② 레시피 시작 실패: {result.get('message')}",
            )
            return

        self._emit_log("info", f"{tag} ② 직전 레시피 시작 → 완료 대기")
        self._notify_chat(f"{tag} ② 직전 레시피 시작: {self.config.recipe_pre_off}")

        self._begin_recipe_wait(SeqState.OFF_PRE_RUNNING)

    def _tick_off_pre_running(self) -> None:
        self._tick_recipe_wait("[자동 OFF]", "②")

    def _do_relay_off(self, tag: str) -> None:
        """자동 OFF 중 릴레이 OFF 후 ③ 직후 레시피 처리."""
        if not self._send_relay_off_with_retry(
            tag,
            retry_count=RELAY_OFF_RETRY_COUNT,
        ):
            # 실패 알림은 호출한 함수에서 이미 전송했습니다.
            self._set_state(SeqState.IDLE)
            return

        if not self.config.recipe_post_off:
            self._emit_log("info", f"{tag} ③ 직후 레시피 사용 안 함")
            self._notify_chat(
                f"{tag} 릴레이 OFF 명령 전송 완료. ③ 레시피 사용 안 함"
            )
            self._set_state(SeqState.IDLE)
            return

        try:
            status = self._ald().get_status()
        except AldClientError as e:
            text = (
                f"{tag} 릴레이 OFF 명령은 전송했습니다.\n"
                f"③ 직후 레시피는 시작하지 않았습니다.\n"
                f"사유: ALD 상태 조회 실패\n상세: {e}"
            )
            self._emit_log("error", text)
            self._notify_chat(text)
            self._set_state(SeqState.IDLE)
            return

        if status["state"] != "idle" or status["alarm"]:
            text = (
                f"{tag} 릴레이 OFF 명령은 전송했습니다.\n"
                f"③ 직후 레시피는 시작하지 않았습니다.\n"
                f"사유: {self._ald_status_reason(status)}"
            )
            self._emit_log("warn", text)
            self._notify_chat(text)
            self._set_state(SeqState.IDLE)
            return

        try:
            csv_path = self._resolve_recipe_path(self.config.recipe_post_off)
            result = self._ald().start_recipe(csv_path)
        except AldClientError as e:
            text = (
                f"{tag} 릴레이 OFF 명령은 전송했습니다.\n"
                f"③ 시작 요청 결과를 확인하지 못했습니다.\n상세: {e}"
            )
            self._emit_log("error", text)
            self._notify_chat(text)
            self._set_state(SeqState.IDLE)
            return

        if result.get("result") != "success":
            msg = result.get("message")
            self._emit_log("error", f"{tag} ③ 레시피 시작 실패: {msg}")
            self._notify_chat(f"{tag} 실패: ③ 레시피 시작 실패 ({msg})")
            self._set_state(SeqState.IDLE)
            return

        self._emit_log("info", f"{tag} ③ 직후 레시피 시작 → 완료 대기")
        self._notify_chat(f"{tag} ③ 직후 레시피 시작: {self.config.recipe_post_off}")

        self._begin_recipe_wait(SeqState.OFF_POST_RUNNING)

    def _tick_off_post_running(self) -> None:
        self._tick_recipe_wait("[자동 OFF]", "③")

    def _begin_recipe_wait(self, state: SeqState) -> None:
        self._recipe_polling_started = time.monotonic()
        self._next_recipe_poll_at = self._recipe_polling_started
        self._recipe_seen_running = False
        self._set_state(state)

    def _finish_recipe_wait_problem(
        self, tag: str, stage: str, reason: str
    ) -> None:
        if stage == "②":
            # 기존 ② 오류/시간 초과 시 OFF 정책 유지.
            # ③ 레시피까지 이어서 시작하지 않습니다.
            self._force_relay_off(tag, f"{stage} {reason}")
            return

        text = (
            f"{tag} {stage} 자동 진행을 종료합니다.\n"
            f"사유: {reason}\n"
            "이 처리에서는 추가 릴레이 명령을 전송하지 않았습니다."
        )
        self._emit_log("error", text)
        self._notify_chat(text)
        self._set_state(SeqState.IDLE)

    def _tick_recipe_wait(self, tag: str, stage: str) -> None:
        now = time.monotonic()
        if now < self._next_recipe_poll_at:
            return

        self._next_recipe_poll_at = now + RECIPE_POLL_INTERVAL_SEC
        elapsed = now - self._recipe_polling_started

        if elapsed > RECIPE_POLL_TIMEOUT_SEC:
            self._finish_recipe_wait_problem(
                tag, stage, "ALD 대기 상태 복귀 확인 시간 초과"
            )
            return

        try:
            status = self._ald().get_status()
        except AldClientError as e:
            self._emit_log(
                "warn", f"{tag} {stage} ALD 상태 조회 실패: {e}"
            )

            if (
                not self._recipe_seen_running
                and elapsed >= RECIPE_START_CONFIRM_TIMEOUT_SEC
            ):
                self._finish_recipe_wait_problem(
                    tag,
                    stage,
                    f"실행 상태를 확인하지 못했습니다. 상태 조회 실패: {e}",
                )
            return

        state = status["state"]

        if status["alarm"] or state == "error":
            self._finish_recipe_wait_problem(
                tag, stage, self._ald_status_reason(status)
            )
            return

        if state == "running":
            if not self._recipe_seen_running:
                self._emit_log(
                    "info", f"{tag} {stage} ALD running 상태 관측"
                )
            self._recipe_seen_running = True
            return

        if not self._recipe_seen_running:
            if elapsed >= RECIPE_START_CONFIRM_TIMEOUT_SEC:
                self._finish_recipe_wait_problem(
                    tag,
                    stage,
                    "시작 요청 후 running 상태를 확인하지 못했습니다. "
                    "미실행인지 짧게 실행 후 종료됐는지는 확인할 수 없습니다.",
                )
            return

        if state != "idle":
            # preheating 등은 대기 복귀로 판단하지 않습니다.
            return

        text = (
            f"{tag} {stage} ALD 실행 상태 관측 후 대기 상태 복귀를 확인했습니다.\n"
            "레시피의 성공 여부는 현재 상태 응답만으로 확인할 수 없습니다."
        )
        self._emit_log("info", text)
        self._notify_chat(text)

        if stage == "②":
            # 기존 대기 복귀 후 OFF 진행 정책 유지
            self._do_relay_off(tag)
        else:
            self._set_state(SeqState.IDLE)