"""AldModule(C# DLL) 통신 클라이언트.

프로토콜 (AldModule.cs와 동일):
  Header: !BBHIQ (16B Big Endian)
    version(1) + flags(1) + cmd_len(2) + body_len(4) + timestamp(8)
  Body: UTF-8 JSON
"""
from __future__ import annotations

import json
import socket
import struct
import time
from typing import Optional

PROTOCOL_VERSION = 1
HEADER_SIZE = 16
HEADER_FORMAT = "!BBHIQ"
MAX_BODY = 10_000_000


class AldClientError(Exception):
    pass


class AldClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7000,
        timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._req_id = 0

    # ---- 내부 ----

    def _next_id(self) -> int:
        self._req_id = (self._req_id + 1) & 0x7FFFFFFF
        return self._req_id

    @staticmethod
    def _build_packet(body_obj: dict) -> bytes:
        body_json = json.dumps(body_obj, ensure_ascii=False)
        body_bytes = body_json.encode("utf-8")
        cmd_str = body_obj.get("command", "") or ""
        cmd_len = len(cmd_str.encode("utf-8"))
        ts = int(time.time())
        header = struct.pack(
            HEADER_FORMAT,
            PROTOCOL_VERSION,
            0,                # flags
            cmd_len,
            len(body_bytes),
            ts,
        )
        return header + body_bytes

    @staticmethod
    def _read_exact(sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise AldClientError("연결이 종료됨")
            buf.extend(chunk)
        return bytes(buf)

    def _request(self, command: str, data: Optional[dict] = None) -> dict:
        body = {"request_id": self._next_id(), "command": command}
        if data is not None:
            body["data"] = data
        packet = self._build_packet(body)

        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(packet)

                header = self._read_exact(sock, HEADER_SIZE)
                version, _flags, _cmd_len, body_len, _ts = struct.unpack(
                    HEADER_FORMAT, header
                )
                if version != PROTOCOL_VERSION:
                    raise AldClientError(f"프로토콜 버전 불일치: {version}")
                if body_len == 0 or body_len > MAX_BODY:
                    raise AldClientError(f"잘못된 body 길이: {body_len}")

                body_bytes = self._read_exact(sock, body_len)
        except (socket.timeout, OSError) as e:
            raise AldClientError(f"TCP 오류: {e}") from e

        try:
            resp = json.loads(body_bytes.decode("utf-8"))
        except Exception as e:
            raise AldClientError(f"JSON 파싱 실패: {e}") from e
        if not isinstance(resp, dict):
            raise AldClientError(f"잘못된 응답 타입: {type(resp).__name__}")
        return resp
    
    @staticmethod
    def _extract_data(resp: dict, command: str) -> dict:
        data = resp.get("data", {})
        if not isinstance(data, dict):
            raise AldClientError(
                f"{command} 응답 data 타입 오류: {type(data).__name__}"
            )
        return data

    # ---- 공개 API ----

    def get_status(self) -> dict:
        resp = self._request("GET_ALD_STATUS")

        if resp.get("command") != "GET_ALD_STATUS_RESULT":
            raise AldClientError("GET_ALD_STATUS 응답 command 불일치")

        data = self._extract_data(resp, "GET_ALD_STATUS")

        if data.get("result") == "fail":
            raise AldClientError(
                data.get("message") or "ALD 상태 조회 실패"
            )

        state = data.get("state")
        if not isinstance(state, str) or state not in {
            "idle", "running", "preheating", "error"
        }:
            raise AldClientError(f"잘못된 ALD state: {state!r}")

        if not isinstance(data.get("alarm"), bool):
            raise AldClientError("ALD alarm 값이 없거나 bool이 아닙니다")

        if not isinstance(data.get("vacuum"), bool):
            raise AldClientError("ALD vacuum 값이 없거나 bool이 아닙니다")

        return data

    def start_recipe(self, csv_path: str) -> dict:
        resp = self._request("START_ALD", {"csv_path": csv_path})
        return self._extract_data(resp, "START_ALD")

    def get_vacuum(self) -> dict:
        resp = self._request("GET_VACUUM")
        return self._extract_data(resp, "GET_VACUUM")

    def get_gate_status(self) -> dict:
        resp = self._request("GET_GATE_STATUS")
        return self._extract_data(resp, "GET_GATE_STATUS")

    def ping(self) -> bool:
        try:
            self.get_status()
            return True
        except AldClientError:
            return False