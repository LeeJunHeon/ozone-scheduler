"""USB 시리얼 릴레이 제어.

LCUS-1 계열 (1채널 CH340 + STM8) 모듈은 펌웨어에 ACK 응답이 없다.
따라서 송신 성공 = 명령 도달로 간주하고, 응답 읽기는 하지 않는다.
실제 릴레이 동작 여부 검증은 불가능 (오존 발생기 동작 여부로 간접 확인).
"""
from __future__ import annotations

import time
from typing import List

import serial
from serial.tools import list_ports

CMD_ON = bytes.fromhex("A0 01 01 A2")
CMD_OFF = bytes.fromhex("A0 01 00 A1")


class RelayError(Exception):
    pass


class RelayClient:
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout  # write timeout 용도. read는 안 함.

    def _send(self, packet: bytes) -> None:
        try:
            with serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            ) as ser:
                ser.write(packet)
                ser.flush()
                # 펌웨어 처리 시간 (datasheet 명시 없음, 경험치 50ms)
                time.sleep(0.05)
        except serial.SerialException as e:
            raise RelayError(str(e)) from e

    def on(self) -> None:
        self._send(CMD_ON)

    def off(self) -> None:
        self._send(CMD_OFF)


def list_serial_ports() -> List[str]:
    """현재 시스템의 시리얼 포트 목록 (예: ['COM3', 'COM5'])."""
    return [p.device for p in list_ports.comports()]