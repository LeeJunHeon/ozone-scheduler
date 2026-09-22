"""오존 스케줄러 메인 윈도우.

UI 목업과 동일한 레이아웃:
  상단 상태바 (릴레이/ALD/알람)
  ├ 수동 제어 │ 자동 스케줄
  레시피 (full width, 3단계) — NAS 폴더 직접 스캔
  ├ 연결 설정 │ 최근 동작
"""
from __future__ import annotations

import os
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import List

from PyQt6.QtCore import Qt, QTime, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from core.ald_client import AldClient, AldClientError
from core.config import AppConfig
from core.relay_client import list_serial_ports
from core.scheduler import OzoneController, SeqState

WEEKDAY_LABELS = ["월", "화", "수", "목", "금", "토", "일"]

logger = logging.getLogger(__name__)
RECIPE_SCAN_TIMEOUT_SEC = 30


# ============ 작은 위젯들 ============


class StatusPill(QLabel):
    """상단 상태 표시 pill."""

    def __init__(self, text: str = "", kind: str = "neutral", parent=None):
        super().__init__(text, parent)
        self.setMinimumHeight(22)
        self._set_style(kind)

    def _set_style(self, kind: str) -> None:
        # kind: 'neutral' | 'success' | 'warn' | 'error'
        colors = {
            "neutral": ("#eeeeee", "#555555"),
            "success": ("#e6f4ea", "#1e8e3e"),
            "warn": ("#fef7e0", "#a35900"),
            "error": ("#fce8e6", "#c5221f"),
        }
        bg, fg = colors.get(kind, colors["neutral"])
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; padding:2px 10px; "
            f"border-radius:11px; font-size:12px;"
        )

    def set(self, text: str, kind: str = "neutral") -> None:
        self.setText(text)
        self._set_style(kind)


class RecipeNameRow(QWidget):
    """레시피 한 줄: 라벨 + 콤보박스(이름 선택). 직접 입력도 가능 (editable)."""

    def __init__(self, idx: int, label: str, parent=None):
        super().__init__(parent)
        self.idx = idx
        self.tag = QLabel(f"  {idx}  {label}")
        self.tag.setStyleSheet("color: #555; font-size: 12px;")

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setMinimumHeight(26)
        self.combo.lineEdit().setPlaceholderText("(사용 안 함)")
        font = QFont("Consolas")
        font.setPointSize(9)
        self.combo.setFont(font)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(4)
        lay.addWidget(self.tag)
        lay.addWidget(self.combo)

    def set_names(self, names: List[str]) -> None:
        """레시피 후보 목록 갱신. 현재 선택값은 보존."""
        current = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("")  # "사용 안 함" 옵션
        for n in names:
            self.combo.addItem(n)
        if current:
            self.combo.setCurrentText(current)
        self.combo.blockSignals(False)

    def set_name(self, name: str) -> None:
        self.combo.setCurrentText(name or "")

    def get_name(self) -> str:
        return self.combo.currentText().strip()


# ============ 메인 윈도우 ============


class MainWindow(QMainWindow):
    _ald_status_result = pyqtSignal(object, object)
    _recipe_scan_result = pyqtSignal(object, object)

    def __init__(self, config: AppConfig, controller: OzoneController):
        super().__init__()
        self.config = config
        self.controller = controller

        self.setWindowTitle("오존 스케줄러 v0.1")
        self.resize(720, 720)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        outer.addLayout(self._build_status_bar())

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addWidget(self._build_manual_box(), 1)
        row1.addWidget(self._build_schedule_box(), 1)
        outer.addLayout(row1)

        outer.addWidget(self._build_recipe_box())

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(self._build_connection_box(), 1)
        row2.addWidget(self._build_log_box(), 1)
        outer.addLayout(row2)

        # 컨트롤러 신호 연결
        self.controller.log.connect(self._on_log)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.relay_state_changed.connect(self._on_relay_changed)

        # 내부 상태
        self._ald_test_in_progress = False
        self._recipe_scan_in_progress = False
        self._recipe_scan_started_at = 0.0
        self._recipe_scan_id = 0

        # 내부 signal 연결
        self._ald_status_result.connect(self._on_ald_status_result)
        self._recipe_scan_result.connect(self._on_recipe_scan_result)

        # ALD 상태를 주기적으로 갱신하는 타이머
        self._ald_status_timer = QTimer(self)
        self._ald_status_timer.setInterval(5000)
        self._ald_status_timer.timeout.connect(self._test_ald_connection)

        # config 로딩 중에는 host/port 변경 이벤트로 테스트하지 않도록 사용
        self._loading_config = True
        self._load_from_config()
        self._loading_config = False

        # 시작 상태
        self.conn_pill.set("확인 중", "neutral")
        self.ald_pill.set("ALD: 확인 중", "neutral")
        self.alarm_pill.set("알람: 확인 중", "neutral")

        # 시작 직후 한 번 확인하고 이후 5초마다 갱신
        QTimer.singleShot(500, self._test_ald_connection)
        self._ald_status_timer.start()

        # 시작 시 레시피 목록 1회 자동 갱신
        QTimer.singleShot(1500, self._refresh_recipes)

        # 다음 실행 시각 표시 (10초)
        self._next_timer = QTimer(self)
        self._next_timer.timeout.connect(self._update_next_run_label)
        self._next_timer.start(10000)

    # ============ 상단 상태바 ============

    def _build_status_bar(self) -> QHBoxLayout:
        h = QHBoxLayout()
        title = QLabel("오존 스케줄러")
        title.setStyleSheet("font-size: 16px; font-weight: 500;")
        h.addWidget(title)
        h.addStretch(1)

        self.relay_pill = StatusPill("릴레이 OFF")
        self.ald_pill = StatusPill("ALD: 확인 중")
        self.alarm_pill = StatusPill("정상", "success")
        h.addWidget(self.relay_pill)
        h.addWidget(self.ald_pill)
        h.addWidget(self.alarm_pill)
        return h

    # ============ 수동 제어 ============

    def _build_manual_box(self) -> QGroupBox:
        box = QGroupBox("수동 제어")
        v = QVBoxLayout(box)
        v.setSpacing(8)

        h = QHBoxLayout()
        self.btn_on = QPushButton("ON")
        self.btn_off = QPushButton("OFF")
        for b in (self.btn_on, self.btn_off):
            b.setMinimumHeight(40)
            f = b.font()
            f.setPointSize(11)
            b.setFont(f)
        self.btn_on.setStyleSheet("color: #1e8e3e;")
        self.btn_off.setStyleSheet("color: #c5221f;")
        self.btn_on.clicked.connect(self._on_manual_on_clicked)
        self.btn_off.clicked.connect(self._on_manual_off_clicked)
        h.addWidget(self.btn_on)
        h.addWidget(self.btn_off)
        v.addLayout(h)

        v.addStretch(1)  # 빈 공간을 위쪽에 배치 → 마지막 동작이 박스 하단에 붙음

        self.last_action_label = QLabel("마지막 동작: -")
        self.last_action_label.setStyleSheet("color: #888; font-size: 11px;")
        self.last_action_label.setWordWrap(True)
        v.addWidget(self.last_action_label)
        return box

    # ============ 자동 스케줄 ============

    def _build_schedule_box(self) -> QGroupBox:
        box = QGroupBox("자동 스케줄")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        h_auto = QHBoxLayout()
        h_auto.addWidget(QLabel("자동 실행"))
        h_auto.addStretch(1)
        self.auto_check = QCheckBox()
        self.auto_check.toggled.connect(self._on_auto_toggled)
        h_auto.addWidget(self.auto_check)
        v.addLayout(h_auto)

        # ON/OFF 시각
        h_on = QHBoxLayout()
        h_on.addWidget(QLabel("ON 시각"))
        h_on.addStretch(1)
        self.on_time = QTimeEdit()
        self.on_time.setDisplayFormat("AP hh:mm")
        self.on_time.timeChanged.connect(self._on_time_changed)
        h_on.addWidget(self.on_time)
        v.addLayout(h_on)

        h_off = QHBoxLayout()
        h_off.addWidget(QLabel("OFF 시각"))
        h_off.addStretch(1)

        self.off_day_label = QLabel("")
        self.off_day_label.setMinimumWidth(65)
        self.off_day_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.off_day_label.setStyleSheet(
            "color: #a35900; font-size: 11px; font-weight: 500;"
        )
        h_off.addWidget(self.off_day_label)

        self.off_time = QTimeEdit()
        self.off_time.setDisplayFormat("AP hh:mm")
        self.off_time.timeChanged.connect(self._on_time_changed)
        h_off.addWidget(self.off_time)

        v.addLayout(h_off)

        # 요일
        h_wd = QHBoxLayout()
        h_wd.setSpacing(3)
        h_wd.addWidget(QLabel("요일"))
        h_wd.addStretch(1)
        self.weekday_btns: List[QPushButton] = []
        wd_group = QButtonGroup(self)
        wd_group.setExclusive(False)
        for i, lab in enumerate(WEEKDAY_LABELS):
            b = QPushButton(lab)
            b.setCheckable(True)
            b.setFixedSize(24, 24)
            f = b.font()
            f.setPointSize(9)
            b.setFont(f)
            b.setStyleSheet(
                "QPushButton { border-radius: 12px; border: 1px solid #ccc; "
                "padding: 0; }"
                "QPushButton:checked { background: #1e8e3e; color: white; "
                "border-color: #1e8e3e; }"
            )
            b.toggled.connect(lambda _checked, idx=i: self._on_weekday_toggled(idx))
            self.weekday_btns.append(b)
            wd_group.addButton(b, i)
            h_wd.addWidget(b)
        v.addLayout(h_wd)

        # 스케줄 요약
        self.schedule_summary_label = QLabel("스케줄 요약: -")
        self.schedule_summary_label.setWordWrap(True)
        self.schedule_summary_label.setStyleSheet(
            "color: #555; font-size: 11px; padding-top: 4px;"
        )
        v.addWidget(self.schedule_summary_label)

        # 다음 실행
        self.next_run_label = QLabel("다음 실행: -")
        self.next_run_label.setStyleSheet(
            "color: #888; font-size: 11px; padding-top: 4px;"
        )
        v.addWidget(self.next_run_label)
        return box

    # ============ 레시피 ============

    def _build_recipe_box(self) -> QGroupBox:
        box = QGroupBox("레시피 (실행 순서)")
        v = QVBoxLayout(box)
        v.setSpacing(2)

        # 헤더: 폴더 경로 + 선택 + 갱신
        header = QHBoxLayout()
        header.addWidget(QLabel("폴더:"))
        self.recipe_dir_edit = QLineEdit()
        self.recipe_dir_edit.setReadOnly(True)
        f_path = QFont("Consolas")
        f_path.setPointSize(8)
        self.recipe_dir_edit.setFont(f_path)
        self.recipe_dir_edit.setStyleSheet("color: #555; background: #f7f7f7;")
        header.addWidget(self.recipe_dir_edit, 1)
        self.recipe_dir_btn = QPushButton("선택")
        self.recipe_dir_btn.setFixedWidth(50)
        self.recipe_dir_btn.clicked.connect(self._on_select_recipe_dir)
        header.addWidget(self.recipe_dir_btn)
        self.refresh_recipes_btn = QPushButton("↻")
        self.refresh_recipes_btn.setFixedWidth(30)
        self.refresh_recipes_btn.clicked.connect(self._refresh_recipes)
        header.addWidget(self.refresh_recipes_btn)
        v.addLayout(header)

        self.rcp_on = RecipeNameRow(1, "ON 직후 실행")
        self.rcp_pre = RecipeNameRow(2, "OFF 직전 실행 (릴레이 켜진 상태)")
        self.rcp_post = RecipeNameRow(3, "OFF 직후 실행 (릴레이 꺼진 상태)")

        for r in (self.rcp_on, self.rcp_pre, self.rcp_post):
            r.combo.editTextChanged.connect(self._on_recipe_changed)
            v.addWidget(r)
        return box

    # ============ 연결 설정 ============

    def _build_connection_box(self) -> QGroupBox:
        box = QGroupBox("연결 설정")
        g = QGridLayout(box)
        g.setVerticalSpacing(6)

        g.addWidget(QLabel("COM 포트"), 0, 0)
        self.com_combo = QComboBox()
        self.com_combo.setMinimumWidth(100)
        self.com_combo.currentTextChanged.connect(self._on_com_changed)
        g.addWidget(self.com_combo, 0, 1)
        self.com_refresh_btn = QPushButton("↻")
        self.com_refresh_btn.setFixedWidth(30)
        self.com_refresh_btn.clicked.connect(self._refresh_ports)
        g.addWidget(self.com_refresh_btn, 0, 2)

        g.addWidget(QLabel("Baudrate"), 1, 0)
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 921600)
        self.baud_spin.setValue(9600)
        self.baud_spin.valueChanged.connect(self._on_baud_changed)
        g.addWidget(self.baud_spin, 1, 1)

        g.addWidget(QLabel("Rayvac TCP"), 2, 0)
        h = QHBoxLayout()
        self.host_edit = QLineEdit()
        self.host_edit.setMaximumWidth(110)
        self.host_edit.editingFinished.connect(self._on_host_port_changed)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(7000)
        self.port_spin.valueChanged.connect(self._on_host_port_changed)
        h.addWidget(self.host_edit)
        h.addWidget(QLabel(":"))
        h.addWidget(self.port_spin)
        g.addLayout(h, 2, 1, 1, 2)

        g.addWidget(QLabel("TCP 상태"), 3, 0)
        self.conn_pill = StatusPill("확인 중")
        g.addWidget(self.conn_pill, 3, 1, 1, 2)

        g.addWidget(QLabel("Chat 알림"), 4, 0)
        self.chat_url_edit = QLineEdit()
        self.chat_url_edit.setPlaceholderText("Google Chat webhook URL (비우면 알림 꺼짐)")
        self.chat_url_edit.editingFinished.connect(self._on_chat_url_changed)
        g.addWidget(self.chat_url_edit, 4, 1, 1, 2)

        g.setRowStretch(5, 1)
        return box

    # ============ 로그 ============

    def _build_log_box(self) -> QGroupBox:
        box = QGroupBox("최근 동작")
        v = QVBoxLayout(box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        f = QFont("Consolas")
        f.setPointSize(9)
        self.log_view.setFont(f)
        self.log_view.document().setMaximumBlockCount(3000)
        v.addWidget(self.log_view)
        return box

    # ============ config 동기화 ============

    def _load_from_config(self) -> None:
        c = self.config

        # COM
        self._refresh_ports(select=c.com_port)
        self.baud_spin.setValue(c.baudrate)

        # Rayvac TCP
        # 초기값을 넣는 과정에서 연결 테스트가 중복 실행되지 않도록 signal 차단
        self.host_edit.blockSignals(True)
        self.port_spin.blockSignals(True)

        self.host_edit.setText(c.ald_host)
        self.port_spin.setValue(c.ald_port)

        self.host_edit.blockSignals(False)
        self.port_spin.blockSignals(False)

        # Schedule
        self.auto_check.setChecked(c.auto_enabled)
        self.on_time.setTime(QTime.fromString(c.on_time, "HH:mm"))
        self.off_time.setTime(QTime.fromString(c.off_time, "HH:mm"))

        for i, b in enumerate(self.weekday_btns):
            if i < len(c.weekdays):
                b.setChecked(c.weekdays[i])

        # Recipe
        self.recipe_dir_edit.setText(c.recipe_dir)
        self.rcp_on.set_name(c.recipe_on)
        self.rcp_pre.set_name(c.recipe_pre_off)
        self.rcp_post.set_name(c.recipe_post_off)

        # Chat
        self.chat_url_edit.setText(c.chat_webhook_url)

        # 스케줄 표시 갱신
        self._update_schedule_summary()
        self._update_next_run_label()

    # ============ 슬롯 (UI → config) ============

    def _on_auto_toggled(self, checked: bool) -> None:
        self.config.auto_enabled = checked
        self.config.save()

    def _on_time_changed(self) -> None:
        self.config.on_time = self.on_time.time().toString("HH:mm")
        self.config.off_time = self.off_time.time().toString("HH:mm")
        self.config.save()

        self._update_schedule_summary()
        self._update_next_run_label()

        if self.config.on_time == self.config.off_time:
            self.schedule_summary_label.setText(
                "⚠ ON 시각과 OFF 시각은 동일하게 설정할 수 없습니다."
            )

    def _on_weekday_toggled(self, idx: int) -> None:
        wds = list(self.config.weekdays)
        while len(wds) < 7:
            wds.append(False)
        wds[idx] = self.weekday_btns[idx].isChecked()
        self.config.weekdays = wds
        self.config.save()
        self._update_schedule_summary()
        self._update_next_run_label()

    def _on_recipe_changed(self) -> None:
        self.config.recipe_on = self.rcp_on.get_name()
        self.config.recipe_pre_off = self.rcp_pre.get_name()
        self.config.recipe_post_off = self.rcp_post.get_name()
        self.config.save()

    def _on_com_changed(self, text: str) -> None:
        if text:
            self.config.com_port = text
            self.config.save()

    def _on_baud_changed(self, v: int) -> None:
        self.config.baudrate = v
        self.config.save()

    def _on_host_port_changed(self) -> None:
        self.config.ald_host = self.host_edit.text().strip() or "127.0.0.1"
        self.config.ald_port = self.port_spin.value()
        self.config.save()

        # 프로그램 초기 config 로딩 중이면 테스트하지 않음
        if getattr(self, "_loading_config", False):
            return

        # 변경된 주소로 연결 테스트 예정
        self.conn_pill.set("확인 중", "neutral")
        self.ald_pill.set("ALD: 확인 중", "neutral")
        self.alarm_pill.set("알람: 확인 중", "neutral")

        # 마지막 변경 후 500ms 뒤 한 번 추가 확인
        QTimer.singleShot(500, self._test_ald_connection)

    def _on_chat_url_changed(self) -> None:
        self.config.chat_webhook_url = self.chat_url_edit.text().strip()
        self.config.save()
        self._on_log("info", "Google Chat URL 설정 저장 완료")

    def _refresh_ports(self, select: str = "") -> None:
        ports = list_serial_ports()
        current = select or self.com_combo.currentText() or self.config.com_port

        self.com_combo.blockSignals(True)
        self.com_combo.clear()
        self.com_combo.addItems(ports)

        if current in ports:
            self.com_combo.setCurrentText(current)
        elif ports:
            self.com_combo.setCurrentIndex(0)
        else:
            self.com_combo.addItem(self.config.com_port)
            self.com_combo.setCurrentText(self.config.com_port)

        self.com_combo.blockSignals(False)

        # blockSignals(True) 상태에서 콤보박스 값을 바꾸면 _on_com_changed()가 호출되지 않는다.
        # 따라서 화면에 최종 표시된 COM 포트를 config에도 직접 반영해야 한다.
        selected = self.com_combo.currentText().strip()
        if selected and selected != self.config.com_port:
            self.config.com_port = selected
            self.config.save()
            logger.info("COM 포트 설정 동기화: %s", selected)

    # ============ 슬롯 (UI → controller) ============

    def _sync_com_port_from_ui(self) -> None:
        """화면에 표시된 COM 포트를 실제 설정값에 강제로 반영한다."""
        selected = self.com_combo.currentText().strip()
        if selected and selected != self.config.com_port:
            self.config.com_port = selected
            self.config.save()
            logger.info("COM 포트 설정 동기화: %s", selected)

    def _on_manual_on_clicked(self) -> None:
        self._sync_com_port_from_ui()
        self.controller.manual_on()
        self.last_action_label.setText(
            f"마지막 동작: {datetime.now():%Y-%m-%d %H:%M:%S}\n[수동 ON] 요청됨"
        )

    def _on_manual_off_clicked(self) -> None:
        self._sync_com_port_from_ui()
        self.controller.manual_off()
        self.last_action_label.setText(
            f"마지막 동작: {datetime.now():%Y-%m-%d %H:%M:%S}\n[수동 OFF] 요청됨"
        )

    # ============ 슬롯 (controller → UI) ============

    @pyqtSlot(str, str)
    def _on_log(self, level: str, text: str) -> None:
        # 파일 기록은 controller에서 이미 완료됨.
        # 여기서는 화면 출력만 처리한다.
        ts = datetime.now().strftime("%H:%M:%S")
        color = {
            "info": "#222",
            "warn": "#a35900",
            "error": "#c5221f",
        }.get(level, "#222")

        self.log_view.append(
            f'<span style="color:#888">{ts}</span> '
            f'<span style="color:{color}">{text}</span>'
        )
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    @pyqtSlot(str)
    def _on_state_changed(self, name: str) -> None:
        is_idle = name == SeqState.IDLE.value

        # 자동 시퀀스 진행 중에는 수동 릴레이 조작 금지
        self.btn_on.setEnabled(is_idle)
        self.btn_off.setEnabled(is_idle)

        # 자동 레시피 진행 중 Rayvac 주소가 바뀌면
        # 완료 확인 대상이 바뀔 수 있으므로 변경 금지
        self.host_edit.setEnabled(is_idle)
        self.port_spin.setEnabled(is_idle)

    @pyqtSlot(bool)
    def _on_relay_changed(self, on: bool) -> None:
        if on:
            self.relay_pill.set("릴레이 ON", "success")
        else:
            self.relay_pill.set("릴레이 OFF", "neutral")

    # ============ 레시피 폴더 / 목록 ============

    def _on_select_recipe_dir(self) -> None:
        start = self.config.recipe_dir or ""
        d = QFileDialog.getExistingDirectory(
            self, "레시피 폴더 선택", start
        )
        if d:
            self.config.recipe_dir = d
            self.config.save()
            self.recipe_dir_edit.setText(d)
            self._refresh_recipes()

    def _refresh_recipes(self) -> None:
        """레시피 폴더의 *.csv 파일 이름(확장자 제외)을 모든 row 콤보박스에 적용."""
        if self._recipe_scan_in_progress:
            elapsed = time.time() - self._recipe_scan_started_at
            if elapsed <= RECIPE_SCAN_TIMEOUT_SEC:
                self._on_log("info", "레시피 폴더 스캔이 이미 진행 중")
                return

            self._on_log(
                "warn",
                f"이전 레시피 폴더 스캔이 {RECIPE_SCAN_TIMEOUT_SEC}초 이상 응답 없음 → 새 스캔 허용",
            )
            self._recipe_scan_in_progress = False

        rdir = self.config.recipe_dir or ""
        if not rdir:
            self._on_log("warn", "레시피 폴더가 설정되지 않음")
            return

        self._recipe_scan_in_progress = True
        self._recipe_scan_started_at = time.time()
        self._recipe_scan_id += 1
        scan_id = self._recipe_scan_id

        def worker() -> None:
            try:
                if not os.path.isdir(rdir):
                    raise OSError(f"레시피 폴더 접근 불가: {rdir}")
                entries = os.listdir(rdir)
                names = sorted(
                    os.path.splitext(f)[0]
                    for f in entries
                    if f.lower().endswith(".csv")
                )
                self._recipe_scan_result.emit((scan_id, names), None)
            except Exception as e:
                logger.exception("레시피 폴더 스캔 실패")
                self._recipe_scan_result.emit((scan_id, None), e)

        threading.Thread(target=worker, name=f"RecipeScan-{scan_id}", daemon=True).start()
        
    @pyqtSlot(object, object)
    def _on_recipe_scan_result(self, payload, error) -> None:
        scan_id, names = payload if isinstance(payload, tuple) else (self._recipe_scan_id, payload)

        if scan_id != self._recipe_scan_id:
            logger.info("stale recipe scan result ignored: %s", scan_id)
            return

        self._recipe_scan_in_progress = False
        if error is not None:
            self._on_log("warn", f"폴더 스캔 실패: {error}")
            return

        names = names or []
        for r in (self.rcp_on, self.rcp_pre, self.rcp_post):
            r.set_names(names)

        self._on_log("info", f"레시피 {len(names)}개 발견")

    # ============ ALD 상태 폴링 (UI 5초마다) ============

    def _test_ald_connection(self) -> None:
        """Rayvac/ALD TCP 연결 상태를 GET_ALD_STATUS 1회로 확인한다."""
        if self._ald_test_in_progress:
            return

        self._ald_test_in_progress = True

        host = self.config.ald_host
        port = self.config.ald_port

        self.conn_pill.set("확인 중", "neutral")
        self.ald_pill.set("ALD: 확인 중", "neutral")
        self.alarm_pill.set("알람: 확인 중", "neutral")

        def worker() -> None:
            try:
                client = AldClient(host, port, timeout=1.5)
                status = client.get_status()

                logger.info(
                    "Rayvac GET_ALD_STATUS 응답 (%s:%s): %s",
                    host,
                    port,
                    status,
                )

                self._ald_status_result.emit(status, None)

            except AldClientError as e:
                logger.warning(
                    "Rayvac 연결 테스트 실패 (%s:%s): %s",
                    host,
                    port,
                    e,
                )
                self._ald_status_result.emit(None, e)

            except Exception as e:
                logger.exception(
                    "Rayvac 연결 테스트 중 예상 밖 예외 (%s:%s)",
                    host,
                    port,
                )
                self._ald_status_result.emit(None, e)

        threading.Thread(
            target=worker,
            name="AldConnectionTest",
            daemon=True,
        ).start()

    @pyqtSlot(object, object)
    def _on_ald_status_result(self, status, error) -> None:
        self._ald_test_in_progress = False

        if error is not None:
            self.conn_pill.set("연결 안 됨", "error")
            self.ald_pill.set("ALD 상태: 확인 불가", "neutral")
            self.alarm_pill.set("알람: 확인 불가", "neutral")
            self._on_log("warn", f"Rayvac TCP 연결 실패: {error}")
            return

        if not isinstance(status, dict):
            self.conn_pill.set("응답 오류", "error")
            self.ald_pill.set("ALD 상태: 응답 오류", "error")
            self.alarm_pill.set("알람: 확인 불가", "neutral")
            self._on_log(
                "error",
                f"ALD 상태 응답 타입 오류: {type(status).__name__}",
            )
            return

        # TCP + GET_ALD_STATUS 응답 정상
        self.conn_pill.set("연결됨", "success")

        state = str(status.get("state", "?")).lower()
        alarm = bool(status.get("alarm", False))
        message = str(status.get("message", "") or "")

        self._on_log(
            "info",
            f"Rayvac 상태 확인: state={state}, alarm={alarm}, message={message}",
        )

        if state == "idle":
            self.ald_pill.set("ALD 상태: idle", "success")
        elif state == "running":
            self.ald_pill.set("ALD 상태: running", "warn")
        elif state == "preheating":
            self.ald_pill.set("ALD 상태: preheating", "warn")
        elif state == "error":
            self.ald_pill.set("ALD 상태: error", "error")
        else:
            self.ald_pill.set(f"ALD 상태: {state}", "neutral")

        # 상태 상세 내용은 마우스를 올리면 확인
        if message:
            self.ald_pill.setToolTip(message)
        else:
            self.ald_pill.setToolTip("")

        if alarm:
            self.alarm_pill.set("알람 발생", "error")
        else:
            self.alarm_pill.set("알람 정상", "success")

    def _record_ui_log(self, level: str, text: str) -> None:
        log_level = {
            "info": logging.INFO,
            "warn": logging.WARNING,
            "error": logging.ERROR,
        }.get(level, logging.INFO)

        logger.log(log_level, text)
        self._on_log(level, text)

    # ============ 다음 실행 라벨 ============
    def _format_qtime_ampm(self, t: QTime) -> str:
        """QTime을 한국어 오전/오후 형식으로 표시한다."""
        hour = t.hour()
        minute = t.minute()
        meridiem = "오전" if hour < 12 else "오후"
        hour12 = hour % 12
        if hour12 == 0:
            hour12 = 12
        return f"{meridiem} {hour12:02d}:{minute:02d}"


    def _update_schedule_summary(self) -> None:
        """자동 스케줄의 의미를 사용자가 헷갈리지 않도록 문장으로 표시한다."""
        on_t = self.on_time.time()
        off_t = self.off_time.time()

        on_str_24 = on_t.toString("HH:mm")
        off_str_24 = off_t.toString("HH:mm")

        on_text = self._format_qtime_ampm(on_t)
        off_text = self._format_qtime_ampm(off_t)

        active_days = [
            WEEKDAY_LABELS[i]
            for i, btn in enumerate(self.weekday_btns)
            if btn.isChecked()
        ]

        if not active_days:
            self.off_day_label.setText("")
            self.schedule_summary_label.setText("스케줄 요약: 선택된 자동 실행 요일이 없습니다.")
            return

        crosses_midnight = off_str_24 <= on_str_24

        days_text = "·".join(active_days)

        if crosses_midnight:
            self.off_day_label.setText("다음날 OFF")

            # 금요일이 선택된 경우 사용자가 가장 헷갈리는 부분을 명확히 표시
            if self.weekday_btns[4].isChecked():
                extra = "※ 금요일 시작분은 토요일에 OFF 됩니다."
            else:
                extra = "※ 선택한 요일의 다음날에 OFF 됩니다."

            self.schedule_summary_label.setText(
                f"스케줄 요약: {days_text} {on_text} 시작 → 다음날 {off_text} 종료\n{extra}"
            )
        else:
            self.off_day_label.setText("같은 날 OFF")
            self.schedule_summary_label.setText(
                f"스케줄 요약: {days_text} {on_text} 시작 → 같은 날 {off_text} 종료"
            )

    def _update_next_run_label(self) -> None:
        if not self.config.auto_enabled:
            self.next_run_label.setText("다음 실행: 자동 모드 OFF")
            return

        now = datetime.now()
        nxt_dt, kind = _compute_next_run(
            now,
            self.config.on_time,
            self.config.off_time,
            self.config.weekdays,
        )
        if nxt_dt is None:
            self.next_run_label.setText("다음 실행: 활성 요일 없음")
            return

        delta = nxt_dt - now
        total = int(delta.total_seconds())
        d, rem = divmod(total, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)

        when_str = _format_datetime_ampm(nxt_dt)

        if d > 0:
            rem_str = f"{d}일 {h}시간 후"
        elif h > 0:
            rem_str = f"{h}시간 {m}분 후"
        else:
            rem_str = f"{m}분 후"

        self.next_run_label.setText(f"다음 실행: {when_str} {kind} ({rem_str})")

    # ============ 종료 ============

    def closeEvent(self, event) -> None:
        logger.info("MainWindow closeEvent called")
        self._on_log("info", "프로그램 창 닫힘 요청 감지")
        self.config.save()
        super().closeEvent(event)


# ============ 다음 실행 계산 ============

def _format_datetime_ampm(dt: datetime) -> str:
    """datetime을 '월 오전 07:00' 형식으로 표시한다."""
    weekday = WEEKDAY_LABELS[dt.weekday()]
    hour = dt.hour
    minute = dt.minute
    meridiem = "오전" if hour < 12 else "오후"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{weekday} {meridiem} {hour12:02d}:{minute:02d}"

def _compute_next_run(
    now: datetime,
    on_str: str,
    off_str: str,
    weekdays: List[bool],
):
    """가장 가까운 ON 또는 OFF 시각 반환. (datetime, 'ON'|'OFF') or (None, '')

    off_time이 on_time보다 같거나 빠르면 OFF는 다음 날짜에 실행되는 것으로 계산한다.
    예: 금요일 07:00 ON / 토요일 00:00 OFF는 금요일 활성 요일에 속한 OFF로 표시한다.
    """
    if not any(weekdays):
        return None, ""

    on_t = QTime.fromString(on_str, "HH:mm")
    off_t = QTime.fromString(off_str, "HH:mm")
    if not on_t.isValid() or not off_t.isValid():
        return None, ""

    candidates = []
    crosses_midnight = off_str <= on_str

    for day_offset in range(0, 8):
        schedule_date = now.date() + timedelta(days=day_offset)
        wd = schedule_date.weekday()  # 월=0
        if wd >= len(weekdays) or not weekdays[wd]:
            continue

        on_dt = datetime(
            schedule_date.year,
            schedule_date.month,
            schedule_date.day,
            on_t.hour(),
            on_t.minute(),
            0,
        )
        if on_dt > now:
            candidates.append((on_dt, "ON"))

        off_date = schedule_date + timedelta(days=1) if crosses_midnight else schedule_date
        off_dt = datetime(
            off_date.year,
            off_date.month,
            off_date.day,
            off_t.hour(),
            off_t.minute(),
            0,
        )
        if off_dt > now:
            candidates.append((off_dt, "OFF"))

    if not candidates:
        return None, ""
    candidates.sort(key=lambda x: x[0])
    return candidates[0]