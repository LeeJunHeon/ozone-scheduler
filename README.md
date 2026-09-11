# Ozone Scheduler

ALD Rayvac 장비에 연결된 USB 시리얼 릴레이를 통해 오존 발생기를 자동 ON/OFF하고,
필요 시 Rayvac 메인 프로그램(`AldModule.cs`)에 레시피 실행을 요청하는 도구.

## 구조

```
ozone_scheduler/
├── main.py
├── settings.json          (첫 실행 시 자동 생성)
├── requirements.txt
├── core/
│   ├── config.py          # AppConfig dataclass + JSON 저장/로드
│   ├── relay_client.py    # USB 시리얼 릴레이 (A0 01 01 A2 / A0 01 00 A1)
│   ├── ald_client.py      # AldModule TCP 클라 (16B BE 헤더 + JSON)
│   └── scheduler.py       # OzoneController (워커 스레드 + 상태머신)
└── ui/
    └── main_window.py     # PyQt6 메인 윈도우
```

## 실행

```bash
pip install -r requirements.txt
python main.py
```

## 시퀀스 정책

### ON (단일 단계)
1. ALD 상태 확인 → idle && !alarm 아니면 스킵
2. 릴레이 ON
3. ① ON 직후 레시피 실행 (있으면)

### OFF (3단계)
1. ALD 상태 확인 → alarm이면 스킵, running이면 최대 5분 idle 대기
2. ② OFF 직전 레시피 실행 → 완료 대기
3. 릴레이 OFF
4. ③ OFF 직후 레시피 실행 → 완료 대기

### 충돌 처리
- 자동 ON 시각, ALD running/alarm → 해당 회차 스킵
- OFF 직전 레시피 실패 → **릴레이 OFF는 그대로 진행** (오존 차단 우선)
- 시퀀스 진행 중 수동 ON/OFF 클릭 → 거부
- 시퀀스 진행 중 다음 ON 시각 도래 → 해당 ON 스킵

## 설정 위치

`settings.json` (앱 폴더). 종료 시 자동 저장.

## 알려진 한계 (TODO)

- 릴레이 응답 코드 검증 미구현 (현재는 송신만 OK로 간주)
- ALD 상태 폴링 실패 시 자동 재연결 로직 없음
- 시스템 트레이 최소화 미지원
- 윈도우 시작 프로그램 등록 미지원
- 일별 반복만 지원, 1회성 스케줄 미지원
