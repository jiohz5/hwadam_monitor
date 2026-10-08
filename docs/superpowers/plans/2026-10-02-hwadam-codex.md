# 화담숲 감시 도구 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 날짜, 복수 회차, 성인 2장·경로 2장·어린이 2장 목표를 GUI에서 변경하고 선택 권종에 1장이라도 가능하면 텔레그램으로 알린다.

**Architecture:** Tkinter GUI, 브라우저 세션과 읽기 전용 상품 조회, 순수 재고 판정, 알림 전송을 분리한다. 전용 Edge 프로필에서 페이지의 조회 헤더를 캡처하고 실제 날짜·회차·옵션을 읽는다. 모노레일 추가구매는 해당 주문의 자격을 검증한다.

**Tech Stack:** Python 3.10 이상, Tkinter, Selenium 4.26 이상, Python 표준 라이브러리, unittest.

**Spec:** `D:/지환/vscode/docs/superpowers/specs/2026-10-02-hwadam-monitor-design.md`

## Global Constraints

- 프로젝트 위치는 `D:/지환/vscode/hwadam_codex`다.
- 기본 날짜는 2026-10-25 일요일, 목표 회차 범위는 08:00부터 08:20까지다.
- 기본 목표는 성인 2장, 경로 2장, 어린이 2장, 청소년 0장이다.
- 목표 수량과 알림 최소 수량을 구분하고 어느 선택 권종이든 1장 가용 시 알린다.
- 상품과 주문의 옵션 번호·한도를 실제 페이지에서 읽는다.
- 공통 재고를 권종별 수량으로 중복 계산하지 않는다.
- 예매와 결제는 사용자가 구매 링크에서 완료한다.
- 기존 프로젝트에는 쓰지 않는다. 텔레그램 원격 명령 수신을 시작하지 않는다.

## Review Focus

- 조회 실패나 누락된 상태를 구매 가능 또는 실제 품절로 오인하지 않는다.
- 새 날짜·상품으로 변경한 뒤 이전 작업의 응답을 GUI에 반영하지 않는다.
- 중지 뒤 늦은 조회 결과로 알림을 보내지 않고 감시 스레드를 중복 실행하지 않는다.
- 텔레그램 실패를 전송 완료로 기록하지 않는다.
- 추가구매 날짜·권종·남은 수량은 실제 주문에 일치해야 한다.

## Task 1 재고 판정과 저장

**Files:** `model.py`, `storage.py`, `tests/test_model.py`, `tests/test_storage.py`.

**Interfaces:** `WatchConfig.validate()`는 검증된 감시 설정을 반환한다. `evaluate_round(raw, items, catalog, config)`는 공통 재고와 권종 상태를 구분하는 `Availability`를 반환한다. `SettingsStore.load/save`는 일반 설정을, `load_telegram_secrets`는 비밀값을 처리한다.

- [x] 성인·경로·어린이 각각 1장일 때 알림, 0장·품절·미확인 상태 제외, 실제 날짜와 범위 경계, 공통 재고 중복 방지를 테스트하고 RED를 확인한다.
- [x] 위 인터페이스를 구현하고 테스트를 GREEN으로 만든다.
- [x] 손상된 설정 복구와 비밀값 분리 저장을 실제 임시 파일로 검증한다.

## Task 2 브라우저와 실제 상품 조회

**Files:** `browser.py`, `api.py`, `tests/test_api.py`.

**Interfaces:** `BrowserSession.connect(url)`가 인증된 조회 환경을 준비한다. `LeisureClient.catalog()`, `schedules()`, `rounds()`, `age_items()`, `verify_order()`가 공개 상품 및 인증된 읽기 응답을 정규화한다. `LeisureClient.check(config, stop)`가 `Snapshot`을 반환한다.

- [x] 두 상품의 서로 다른 옵션 번호, HTTP 오류, 주문 날짜 불일치·권종 한도, 모노레일의 구간·입장 시각·권종 조건을 테스트하고 RED를 확인한다.
- [x] 전용 Edge 포트 9444와 자체 프로필을 구성한다. 기존 저장 세션은 필요한 경우 읽어 복원하며 원본에는 쓰지 않는다.
- [x] 조회 헤더를 XHR와 fetch에서 캡처하고 읽기 요청만 수행한다. 토큰을 로그에 출력하지 않는다.
- [x] 후보 회차의 권종을 검증하고 별도 모노레일 조회 실패가 입장권 결과를 막지 않게 한다.
- [ ] 실제 10월 3일 상품과 10월 25일 상품에서 읽기 조회를 실행해 응답 형식을 확인한다. (전용 Edge 로그인 대기)

## Task 3 감시와 텔레그램

**Files:** `monitor.py`, `telegram_bot.py`, `tests/test_monitor.py`, `tests/test_telegram.py`.

**Interfaces:** `AlertTracker.pending(snapshot)`와 `ack(snapshot)`는 성공한 알림만 중복 억제한다. `MonitorRunner.start/stop`은 하나의 감시 루프와 중지 이벤트를 관리한다. `TelegramNotifier.send(text)`는 전송 성공 또는 명확한 실패를 반환한다.

- [x] 첫 발견, 유지 상태 억제, 품절 뒤 재등장, 수량 증가, 실패 재시도, 중지 뒤 알림 억제를 테스트하고 RED를 확인한다.
- [x] 감시 루프, 요청 직렬화, 일시 오류의 대기시간 증가, 세션 만료 표시를 구현한다.
- [x] 알림에 날짜·요일·권종 목표·공통 재고·모노레일 상태·구매 링크를 넣는다.

## Task 4 GUI와 실행 도구

**Files:** `gui.py`, `main.py`, `run_hwadam.bat`, `requirements.txt`, `.gitignore`, `helper_secrets.env.example`, `README.md`, `tests/test_gui.py`.

**Interfaces:** `App(root, base_dir)`는 제품 선택·날짜·두 회차 목록·권종 수량·텔레그램 설정을 제공한다. 큐와 메인 스레드에서만 GUI를 갱신한다. `main.py --gui-smoke`는 브라우저와 알림 없이 GUI 기본 구성 및 레이아웃을 검증한다. `main.py --probe`는 일반 설정을 덮어쓰거나 알림을 보내지 않고 실제 조회를 검증한다.

- [x] 날짜·권종 목표의 설정 변환과 늦은 결과 제외를 테스트하고 RED를 확인한다.
- [x] 날짜에 요일을 표시하고 품절 회차도 복수 선택 가능하게 한다. 입장권·모노레일의 회차를 각각 표시한다.
- [x] 모드별 설정, 조회·시작·중지, 상태, 로그, 알림 테스트와 저장을 연결한다.
- [x] 전체 unittest 38개, Python 컴파일, GUI smoke를 검증한다.
- [ ] 인증된 실제 읽기 조회와 주문 추가구매 조회를 검증한다. (로그인·주문 링크 필요)
- [x] 새로운 검토 에이전트가 전체 구현을 검토하고 중요한 발견은 회귀 테스트와 함께 수정한다.

## 작업 환경

상위 폴더는 Git 저장소가 아니다. 사용자가 지정한 새 폴더에서 작업하므로 기존 브랜치 변경이나 worktree 생성은 필요하지 않다. 작업 기록은 `docs/implementation-progress.md`에 남긴다. 사용자의 제작 지시에 따라 이 세션에서 구현하고 실행 가능한 산출물을 검증한다.
