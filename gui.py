"""화담숲 감시 모델·감시 루프·브라우저 예약·관리 GUI 통합.

수정 위치: 1. 모델/재고 판정 / 2. 감시/알림 / 3. 예약 테스트 / 4. 관리 화면 / 5. 앱 제어.
프로그램 실행은 main.py 또는 run_hwadam.vbs를 사용한다.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import math
import re
from urllib.parse import parse_qs, urlsplit
import datetime as dt
import logging
import json
import hashlib
from pathlib import Path
import queue
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from telegram_bot import TelegramNotifier
from ui_widgets import BG, WHITE, GREEN, MUTED, card, TimeGrid
import os
import time
from typing import Callable
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException,  TimeoutException, ElementClickInterceptedException


# === 1. 감시 모델·재고 판정 ===

AGE_NAMES = {"ADULT": "성인", "SENIOR": "경로", "TEEN": "청소년", "CHILD": "어린이"}
SECTION_NAMES = {"MONORAIL_SEGMENT_1": "1구간(1-2)", "MONORAIL_SEGMENT_2": "2구간(1-3)", "MONORAIL_SEGMENT_3": "순환(1-1)"}
DEFAULT_URL = "https://leisure-web.yanolja.com/leisure/10367229"
REFERENCE_URL = "https://leisure-web.yanolja.com/leisure/10323843"
KST = dt.timezone(dt.timedelta(hours=9))
OPEN_CODES = {"IN_SALE", "AVAILABLE", "OPEN"}
CLOSED_CODES = {"SOLD_OUT", "END_OF_SALE", "WAITING_FOR_SALE", "CLOSED"}


def date_label(value: str) -> str:
    day = dt.date.fromisoformat(value)
    return f"{value}({'월화수목금토일'[day.weekday()]})"


def parse_time_specs(text: str) -> tuple[str, ...]:
    return tuple(value for value in re.split(r"[,\s]+", re.sub(r"\s*~\s*", "~", text.strip())) if value)


def _valid_time(value: str) -> bool:
    return isinstance(value,str) and bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value))


def time_matches(value: str, specs: tuple[str, ...]) -> bool:
    if not _valid_time(value):
        return False
    for spec in specs:
        start, separator, end = spec.partition("~")
        if separator and start <= value <= end or not separator and value == start:
            return True
    return False


def relative_monorail_specs(entry_time: str) -> tuple[str, ...]:
    """입장 20분 후를 중심으로 ±15분, 같은 이용 날짜 안에서만 확인한다."""
    if not _valid_time(entry_time): raise ValueError('입장 시각을 확인하세요')
    hour,minute=map(int,entry_time.split(':'))
    center=hour*60+minute+20
    start=max(0,center-15);end=min(1439,center+15)
    if start>end: return ()
    return (f'{start//60:02d}:{start%60:02d}~{end//60:02d}:{end%60:02d}',)


def validate_url(value: str, *, order=False) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.hostname != "leisure-web.yanolja.com" or parsed.username or parsed.password or parsed.port:
        raise ValueError("NOL 상품 주소는 https://leisure-web.yanolja.com 주소여야 합니다")
    if not re.fullmatch(r"/leisure/\d+/?", parsed.path):
        raise ValueError("/leisure/상품번호 형태의 상품 주소를 입력하세요")
    if order and not parse_qs(parsed.query).get("orderGroupId", [""])[0].strip():
        raise ValueError("주문 내역에서 연 추가구매 주소(orderGroupId 포함)가 필요합니다")
    return value.strip()


@dataclass(frozen=True)
class WatchConfig:
    mode: str = "entry"
    product_url: str = DEFAULT_URL
    addon_url: str = ""
    date: str = "2026-10-25"
    time_specs: tuple[str, ...] = ("08:00~08:20",)
    counts: dict[str, int] = field(default_factory=lambda: {"ADULT": 2, "SENIOR": 2, "TEEN": 0, "CHILD": 2})
    section: str = "MONORAIL_SEGMENT_2"
    include_monorail: bool = False
    mono_specs: tuple[str, ...] = ()
    mono_relative: bool = False
    interval: float = 2.0
    repeat_seconds: float = 0.0

    @property
    def url(self):
        return self.addon_url if self.mode == "addon" else self.product_url

    @property
    def selected_ages(self):
        return tuple(age for age in AGE_NAMES if self.counts.get(age, 0) > 0)

    @property
    def total(self):
        return sum(self.counts.get(age, 0) for age in AGE_NAMES)

    def monorail_specs_for(self,entry_time):
        return relative_monorail_specs(entry_time) if self.mono_relative else self.mono_specs

    def validate(self):
        if self.mode not in ("entry", "addon"):
            raise ValueError("감시 모드를 선택하세요")
        if type(self.mono_relative) is not bool: raise ValueError('모노레일 상대 시간 설정을 확인하세요')
        dt.date.fromisoformat(self.date)
        validate_url(self.product_url)
        if self.mode == "addon":
            validate_url(self.addon_url, order=True)
        if not self.time_specs:
            raise ValueError("감시할 회차를 하나 이상 선택하세요")
        for spec in (*self.time_specs, *self.mono_specs):
            start, separator, end = spec.partition("~")
            if not _valid_time(start) or separator and (not _valid_time(end) or start > end):
                raise ValueError("시간은 08:00, 08:20 또는 08:00~08:20 형식으로 입력하세요")
        if any(age not in AGE_NAMES for age in self.counts):
            raise ValueError("알 수 없는 권종입니다")
        if any(type(value) is not int or value < 0 or value > 999 for value in self.counts.values()) or self.total == 0:
            raise ValueError("권종 수량은 0~999 정수이며 한 권종 이상을 선택해야 합니다")
        if self.section not in SECTION_NAMES:
            raise ValueError("모노레일 구간을 선택하세요")
        if not math.isfinite(float(self.interval)) or not 2 <= float(self.interval) <= 300:
            raise ValueError("조회 주기는 2~300초로 입력하세요")
        if not math.isfinite(float(self.repeat_seconds)) or self.repeat_seconds < 0 or 0 < self.repeat_seconds < 10:
            raise ValueError("반복 알림은 0(변화 시만) 또는 10초 이상으로 입력하세요")
        return self


@dataclass(frozen=True)
class Group:
    id: int
    code: str
    start: str
    end: str
    limit: int | None
    age_option_id: int | None
    ages: dict[int, str]
    sections: dict[str, int]
    section_option_id: int | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Catalog:
    id: int
    name: str
    groups: dict[str, Group]
    additional_only: bool = False


def parse_product(product: dict) -> Catalog:
    if not isinstance(product, dict) or not product.get("productId"):
        raise ValueError("상품 정보를 읽지 못했습니다")
    groups = {}
    for raw in product.get("productOptionGroups") or []:
        code = raw.get("productOptionGroupAttributeTypeCode")
        if not code and product.get("isAdditionalPurchaseOnly"):
            code = "MONORAIL"
        if not code:
            continue
        ages, sections = {}, {}
        age_option = section_option = None
        for option in raw.get("productOptions") or []:
            attribute = option.get("productOptionAttributeCode")
            if attribute == "AGE":
                age_option = option["productOptionId"]
            if attribute == "MONORAIL_SEGMENT":
                section_option = option["productOptionId"]
            for item in option.get("productOptionItems") or []:
                detail = item.get("productOptionAttributeDetailCode")
                if attribute == "AGE" and detail in AGE_NAMES:
                    ages[item["productOptionItemId"]] = detail
                if detail in SECTION_NAMES:
                    sections[detail] = item["productOptionItemId"]
                elif attribute == "MONORAIL_SEGMENT":
                    for key, label in SECTION_NAMES.items():
                        if item.get("productOptionItemName") == label:
                            sections[key] = item["productOptionItemId"]
        groups[code] = Group(raw["productOptionGroupId"], code, raw.get("reservationStartDate") or "",
                             raw.get("reservationEndDate") or "", raw.get("quantityPerPerson"),
                             age_option, ages, sections, section_option, raw)
    if not groups:
        raise ValueError("화담숲 상품 옵션을 찾지 못했습니다")
    return Catalog(product["productId"], product.get("productName", "화담숲"), groups,
                   bool(product.get("isAdditionalPurchaseOnly")))


def _quantity(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("재고 수량 형식이 잘못되었습니다")
    number = int(value)
    if str(value) not in (str(number), f"{number}.0") or number < 0:
        raise ValueError("재고 수량 형식이 잘못되었습니다")
    return number


@dataclass(frozen=True)
class Availability:
    time: str
    category: str
    quantity: int | None
    eligible_ages: tuple[str, ...]
    full_target: bool
    reason: str = ""
    section: str = ""
    age_quantities: tuple[tuple[str,int | None], ...] = ()
    entry_time: str = ""

    @property
    def available(self):
        return bool(self.eligible_ages) and self.quantity != 0 and not self.reason

    @property
    def key(self):
        return (self.category, self.time, self.section, self.entry_time)

    @property
    def stock_label(self):
        if not self.available:
            return self.reason or "구매 가능한 선택 권종 없음"
        return f"공통 잔여 {self.quantity}장" if self.quantity is not None else "구매 가능 · 잔여 수량 미공개"


def evaluate_round(raw: dict, items: list[dict], catalog: Group, config: WatchConfig,
                   category: str, *, allowed_ages=None) -> Availability:
    time = raw.get("time", "")
    section = config.section if category == "monorail" else ""
    try:
        quantity = _quantity(raw.get("inventoryQuantity"))
    except (ValueError, TypeError, OverflowError):
        return Availability(time, category, None, (), False, "재고 수량 확인 필요",section)
    status = raw.get("roundStatusCode")
    reason = "품절" if status == "SOLD_OUT" or quantity == 0 else ""
    if status not in OPEN_CODES and not reason:
        reason = "판매 중 아님" if status in CLOSED_CODES else "회차 상태 확인 필요"
    eligible, quantities = set(), {}
    allowed = set(config.selected_ages if allowed_ages is None else allowed_ages) & set(config.selected_ages)
    for item in items:
        age = catalog.ages.get(item.get("productOptionItemId"))
        if age not in allowed:
            continue
        item_status = item.get("productOptionItemStatusCode")
        round_status = item.get("roundStatusCode")
        if item.get('isDisabled') is True:
            continue
        statuses=[value for value in (item_status,round_status) if value is not None]
        if not statuses or any(value not in OPEN_CODES for value in statuses):
            continue
        try:
            item_quantity = _quantity(item.get("inventoryQuantity"))
        except (ValueError, TypeError, OverflowError):
            continue
        if item_quantity == 0:
            continue
        eligible.add(age)
        quantities[age] = item_quantity
    ordered = tuple(age for age in AGE_NAMES if age in eligible)
    full = (not reason and quantity is not None and quantity >= config.total
            and set(config.selected_ages) <= eligible
            and all(quantities.get(age) is None or quantities[age] >= config.counts[age] for age in config.selected_ages))
    return Availability(time, category, quantity, ordered, full, reason, section,
                        tuple((age,quantities.get(age)) for age in ordered))


@dataclass(frozen=True)
class Snapshot:
    config: WatchConfig
    rounds: tuple[Availability, ...]
    monorail: tuple[Availability, ...] = ()
    monorail_error: str = ""
    checked_at: str = field(default_factory=lambda: dt.datetime.now(KST).strftime("%H:%M:%S"))

    @property
    def hits(self):
        return tuple(result for result in self.rounds if result.available)


def count_label(counts):
    return " · ".join(f"{AGE_NAMES[age]} {counts.get(age, 0)}장" for age in AGE_NAMES if counts.get(age, 0))


# === 2. 감시 루프·알림 ===

def available_rows(snapshot):
    return (*snapshot.hits,*(row for row in snapshot.monorail if row.available))


@dataclass
class _Record:
    last: object
    sent_at: float | None = None
    dirty: bool = True


class AlertTracker:
    def __init__(self,clock=time.monotonic):
        self.records={}
        self.clock=clock

    def pending(self,snapshot: Snapshot):
        rows=available_rows(snapshot)
        keys={row.key for row in rows}
        uncertain={row.key for row in (*snapshot.rounds,*snapshot.monorail) if '확인 필요' in row.reason}
        self.records={key:record for key,record in self.records.items()
                      if key in keys or key in uncertain or snapshot.monorail_error and key[0]=='monorail'}
        pending=[]
        now=self.clock()
        for row in rows:
            record=self.records.get(row.key)
            if record is None:
                record=self.records[row.key]=_Record(row)
            else:
                before=record.last
                increased=(row.quantity is not None and before.quantity is not None and row.quantity>before.quantity)
                previous=dict(before.age_quantities)
                increased |= any(number is not None and previous.get(age) is not None and number>previous[age]
                                 for age,number in row.age_quantities)
                new_age=bool(set(row.eligible_ages)-set(before.eligible_ages))
                record.dirty |= increased or new_age or row.full_target and not before.full_target
                record.last=row
            repeat=snapshot.config.repeat_seconds
            if record.dirty or record.sent_at is None or repeat and now-record.sent_at>=repeat:
                pending.append(row)
        return tuple(pending)

    def ack(self,snapshot: Snapshot):
        for row in available_rows(snapshot):
            self.records[row.key]=_Record(row,self.clock(),False)


def format_alert(snapshot: Snapshot):
    from storage import sanitize
    config=snapshot.config
    title='화담숲 모노레일 추가구매' if config.mode=='addon' else '화담숲 입장권'
    lines=[f'🌳 {title} 구매 가능',date_label(config.date),'목표: '+count_label(config.counts),
           '선택 권종에 1장이라도 가능한 회차를 알립니다.']
    for row in snapshot.hits:
        label='모노레일' if row.category=='monorail' else '입장권'
        lines.extend([f'\n{label} {row.time} · {row.stock_label}',
                      '가능 권종: '+', '.join(AGE_NAMES[age] for age in row.eligible_ages),
                      '목표 전량 가능' if row.full_target else '일부 구매 가능 · 목표 전량 확보는 별도 확인'])
        known=[f'{AGE_NAMES[age]} {number}장' for age,number in row.age_quantities if number is not None]
        if known: lines.append('권종별 조회 잔여: '+', '.join(known)+' (공통 잔여 내에서 구매)')
    if config.mode=='addon' or config.include_monorail:
        lines.append('\n구간: '+SECTION_NAMES[config.section])
    if config.include_monorail and config.mode=='entry':
        if config.mono_relative: lines.append('모노레일 시간: 각 입장 시각 +20분 ±15분')
        mono=[row for row in snapshot.monorail if row.available]
        if mono:
            lines.append('모노레일 후보:')
            lines.extend(f'{row.time} ({row.entry_time} 입장 기준) · {row.stock_label} · '+', '.join(AGE_NAMES[age] for age in row.eligible_ages) for row in mono)
        elif snapshot.monorail_error:
            lines.append('모노레일 확인 필요: '+sanitize(snapshot.monorail_error))
        else:
            lines.append('모노레일: 조건에 맞는 구매 가능 후보 없음')
    lines.extend(['\n조회: '+snapshot.checked_at+' (한국 시간)',config.url])
    return '\n'.join(lines)


class MonitorRunner:
    def __init__(self,client,emit):
        self.client=client
        self.emit=emit
        self.thread=None
        self.stop_event=threading.Event()
        self.guard=threading.Lock()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self,config,notifier):
        config.validate()
        with self.guard:
            if self.running:
                return False
            self.stop_event=threading.Event()
            self.thread=threading.Thread(target=self._run,args=(config,notifier,self.stop_event),daemon=True,name='hwadam-monitor')
            self.thread.start()
            return True

    def stop(self):
        self.stop_event.set()

    def _event(self,kind,**values):
        self.emit({'kind':kind,**values})

    def _run(self,config,notifier,stop):
        from api import AuthLost, Cancelled
        from storage import sanitize
        tracker=AlertTracker()
        failures=0
        checks=0
        reason='user_stop'
        try:
            self._event('started',config=config)
            while not stop.is_set():
                delay=config.interval
                phase='check'
                try:
                    result=self.client.check(config,stop)
                    if stop.is_set(): break
                    checks+=1
                    self._event('snapshot',snapshot=result,checks=checks)
                    if tracker.pending(result):
                        if stop.is_set(): break
                        phase='telegram'
                        notifier.send(format_alert(result))
                        tracker.ack(result)
                        self._event('sent',message='가용 회차 텔레그램 알림 전송 완료',snapshot=result,hits=len(result.hits))
                        # 즉시 예약 시도
                        self._event('booking_attempt',message='가용 회차 발견 · 예약 시도',snapshot=result)
                        try:
                            # GUI 쪽에서 브라우저 예약 로직을 실행하도록 이벤트만 발생
                            # 실제 예약은 gui.py의 run_test_booking을 호출하는 핸들러에서 처리
                            pass
                        except Exception as be:
                            self._event('error',message='예약 시도 중 오류 · '+sanitize(be),phase='booking')
                        # 수량 미달이면 계속 감시 (루프 유지)
                        self._event('log',message='예약 시도 후 감시 계속 (수량 미달 시 재시작)')
                    failures=0
                except Cancelled:
                    reason='user_stop' if stop.is_set() else 'cancelled'
                    break
                except AuthLost as problem:
                    reason='auth_lost'
                    self._event('auth',message=sanitize(problem),phase=phase,failures=failures+1,retry_seconds=0)
                    if not stop.is_set():
                        try:
                            notifier.send('화담숲 감시가 중지되었습니다. NOL 로그인 후 다시 시작하세요.\n'+date_label(config.date))
                        except Exception as error:
                            self._event('error',message=sanitize(error),phase='telegram',failures=1,retry_seconds=0)
                    break
                except Exception as problem:
                    failures+=1
                    delay=min(300,max(config.interval,10)*(2**min(failures-1,4)))
                    self._event('error',message=sanitize(problem)+f' · {delay:g}초 후 재시도',
                                phase=phase,failures=failures,retry_seconds=delay)
                stop.wait(delay)
        finally:
            self._event('finished',message='감시 종료',reason=reason,checks=checks)


# === 3. 브라우저 예약 테스트 ===

LogFn = Callable[[str], None]


class BookingTestError(RuntimeError):
    pass


def run_test_booking(driver, config: WatchConfig, *, log: LogFn = print, age_label: str = "성인",
                     price_keyword: str = "일반", stop_before_pay: bool = True,
                     should_stop: Callable[[], bool] | None = None) -> bool:
    """브라우저에 선택된 날짜에서 첫 시각·한 권종을 시험하고 결제 직전에 끝낸다.

    날짜·수량은 사용자가 브라우저에서 먼저 설정한다. 최종 결제는 지원하지 않는다.
    """
    # stop_before_pay=False 이면 실제 결제까지 진행 (감시 자동 예약용)
    if not stop_before_pay:
        pass  # 실제 결제 허용

    def checkpoint():
        if should_stop is not None and should_stop():
            raise BookingTestError('프로그램 종료 요청으로 예약 테스트를 중지했습니다')

    def step(message):
        checkpoint()
        now = dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]  # ms 단위
        log(f'[{now}] [TEST-BOOKING] {message}')

    def click(element):
        checkpoint()
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});",element)
        checkpoint()
        try: element.click()
        except ElementClickInterceptedException:
            checkpoint()
            driver.execute_script('arguments[0].click();',element)

    checkpoint()
    config.validate()
    if config.mode!='entry': raise BookingTestError('예약 테스트는 입장권 모드만 지원합니다')
    wait=WebDriverWait(driver,15)
    stage='옵션 선택하기'
    target_time=config.time_specs[0].split('~')[0].strip()
    try:
        step(f'대상 날짜 {config.date} · 첫 시각 {target_time} · 권종 {age_label}')
        # 달력이 나오려면 "옵션 선택하기" 버튼을 먼저 눌러야 함
        step('옵션 선택하기 버튼 대기')
        try:
            opt_btn = WebDriverWait(driver, 8).until(EC.element_to_be_clickable(
                (By.XPATH, '//button[.//span[contains(text(), "옵션 선택하기")] or contains(., "옵션 선택하기")]')
            ))
            click(opt_btn)
            step('옵션 선택하기 클릭 완료 · 달력 대기')
            time.sleep(0.6)
        except TimeoutException:
            step('옵션 선택하기 버튼을 찾지 못했습니다. 이미 열려 있다고 가정하고 진행합니다.')

        stage='날짜 선택'
        day = config.date.split('-')[-1].lstrip('0') or '0'
        date_clicked = False
        for attempt in range(1, 6):
            try:
                candidates = driver.find_elements(By.XPATH,
                    f'//button[normalize-space(.)="{day}" or contains(@aria-label, "{day}") or contains(., "{day}")]')
                for btn in candidates:
                    txt = (btn.text or '').strip()
                    al = (btn.get_attribute('aria-label') or '')
                    if txt == day or day in al or day in (btn.text or ''):
                        if btn.is_enabled() and btn.get_attribute('aria-disabled') != 'true':
                            click(btn)
                            date_clicked = True
                            step(f'날짜 선택 완료: {config.date} (시도 {attempt}, 텍스트={txt})')
                            time.sleep(0.7)
                            break
                if date_clicked:
                    break
            except Exception:
                pass
            step(f'날짜 시도 {attempt}/5 · {day}일 미발견 · 0.4초 후 재시도')
            time.sleep(0.4)

        if not date_clicked:
            step('날짜 버튼을 찾지 못했습니다. 이미 선택된 상태로 진행합니다.')

        stage='시간 선택'
        step(f'시간 버튼 대기 중 · 대상 {target_time}')
        clicked = False
        max_try = 8
        for attempt in range(1, max_try + 1):
            buttons = driver.find_elements(By.CSS_SELECTOR, 'button[role="option"]')
            found = []
            for button in buttons:
                try:
                    label = (button.get_attribute('aria-label') or '').strip()
                    text = (button.text or '').strip()
                    inners = [p.text.strip() for p in button.find_elements(By.TAG_NAME, 'p') if p.text and p.text.strip()]
                    combined = ' '.join([label, text] + inners)
                    if target_time in combined:
                        found.append(combined[:50])
                    if target_time not in combined:
                        continue
                    if any(x in combined for x in ('품절', '매진', '대기', 'SOLD')):
                        continue
                    if button.get_attribute('aria-disabled') == 'true' or not button.is_enabled():
                        continue
                    click(button)
                    clicked = True
                    step(f'시간 선택 완료: {target_time} (시도 {attempt})')
                    break
                except Exception:
                    continue
            if clicked:
                break
            step(f'시도 {attempt}/{max_try} · {target_time} 미발견 (후보 {len(found)}개) · 0.4초 후 재시도')
            time.sleep(0.4)

        if not clicked:
            raise BookingTestError(f'구매 가능한 {target_time} 시간 버튼을 찾지 못했습니다')
        time.sleep(0.15)

        stage='권종 선택';step(f'권종 선택: 입장권 {age_label}')
        options=wait.until(EC.presence_of_all_elements_located(
            (By.XPATH,f'//button[contains(., "입장권 {age_label}") or contains(., "{age_label}")]')))
        for option in options:
            if age_label in (option.text or '') and option.is_enabled():
                click(option);break
        else: raise BookingTestError(f'권종 {age_label} 버튼을 찾지 못했습니다')
        time.sleep(0.12)

        stage='요금 선택';step('요금 옵션 선택: '+price_keyword)
        price_clicked = False
        for attempt in range(1, 6):
            try:
                prices = driver.find_elements(By.XPATH, f'//button[contains(., "{price_keyword}")]')
                for button in prices:
                    text = button.text or ''
                    if price_keyword in text and ('원' in text or '일반' in text) and button.is_enabled():
                        click(button)
                        price_clicked = True
                        step(f'요금 선택 완료 (시도 {attempt})')
                        break
                if price_clicked:
                    break
            except StaleElementReferenceException:
                pass
            step(f'요금 시도 {attempt}/5 · 미발견 · 0.35초 후 재시도')
            time.sleep(0.35)
        if not price_clicked:
            raise BookingTestError(f'{price_keyword} 요금 옵션을 찾지 못했습니다')
        time.sleep(0.15)

        stage='구매 화면 이동';step('구매하기 버튼 클릭')
        buy=wait.until(EC.element_to_be_clickable((By.XPATH,'//button[contains(., "구매하기")]')))
        click(buy);time.sleep(0.45)

        stage='결제수단 선택';step('결제수단: NOL 머니 선택')
        money_clicked = False
        for attempt in range(1, 6):
            try:
                money = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((By.XPATH,
                    '//button[contains(., "NOL 머니")] | //label[contains(., "NOL 머니")] | //span[contains(text(), "NOL 머니")] | //div[contains(., "NOL 머니")]')))
                click(money)
                money_clicked = True
                step(f'NOL 머니 선택 완료 (시도 {attempt})')
                break
            except StaleElementReferenceException:
                step(f'NOL 머니 시도 {attempt}/5 · Stale · 재시도')
                time.sleep(0.3)
            except TimeoutException:
                step(f'NOL 머니 시도 {attempt}/5 · 미발견 · 재시도')
                time.sleep(0.4)
        if not money_clicked:
            raise BookingTestError('NOL 머니 결제수단을 선택하지 못했습니다')
        time.sleep(0.2)

        stage='전체 동의';step('전체 동의 항목 확인')
        xpath = (
            "//div[normalize-space(text())='전체 동의']"
            "/ancestor::div[.//input[@type='checkbox']][1]"
            "//input[@type='checkbox']"
        )
        try:
            checkbox = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
        except TimeoutException:
            raise BookingTestError('전체 동의 체크박스를 찾지 못했습니다')
        # 이미 동의되어 있으면 다시 눌러 해제하지 않기
        if not checkbox.is_selected():
            click(checkbox)
        # 실제 체크 상태 확인
        try:
            WebDriverWait(driver, 5).until(
                EC.element_located_to_be_selected((By.XPATH, xpath))
            )
        except TimeoutException:
            raise BookingTestError('전체 동의 체크 후에도 선택 상태가 되지 않았습니다')
        step('전체 동의 체크 완료')
        time.sleep(0.08)
        # 확인
        if not checkbox.is_selected():
            try:
                click(checkbox)
            except Exception:
                pass
        step('전체 동의 체크 완료 (JS 강제)')
        time.sleep(0.1)

        stage='최종 결제 버튼 클릭'
        step('결제하기 버튼 대기 및 클릭')
        # 클릭 전 창 핸들 저장
        original_handles = set(driver.window_handles)
        original_window = driver.current_window_handle
        step(f'클릭 전 창 수: {len(original_handles)}')

        pay_xpath = (
            "//button[.//span[contains(normalize-space(.), '결제하기')]]"
            "[not(@aria-disabled='true') and not(@data-state='loading')]"
        )
        def visible_pay_button(drv):
            for button in drv.find_elements(By.XPATH, pay_xpath):
                try:
                    if button.is_displayed() and button.is_enabled():
                        return button
                except StaleElementReferenceException:
                    continue
            return False

        try:
            pay_button = WebDriverWait(driver, 12).until(visible_pay_button)
        except TimeoutException:
            raise BookingTestError('표시된 활성 결제 버튼을 찾지 못했습니다')
        btn_text = (pay_button.text or '').strip().replace('\n', ' ')
        step(f'결제 버튼 발견: {btn_text[:60]}')
        click(pay_button)
        step('결제하기 버튼 클릭 완료 · 새 결제 창 대기')

        # 새 창이 뜰 때까지 대기
        new_window = None
        deadline = time.time() + 12
        while time.time() < deadline:
            handles = set(driver.window_handles)
            new_ones = handles - original_handles
            if new_ones:
                for h in new_ones:
                    try:
                        driver.switch_to.window(h)
                        url = driver.current_url or ''
                        step(f'새 창 발견: {url[:80]}')
                        if 'nol-money' in url or 'yanolja' in url:
                            new_window = h
                            break
                    except Exception:
                        continue
                if new_window:
                    break
            time.sleep(0.08)

        if not new_window:
            step('새 결제 창을 찾지 못했습니다. 현재 창에서 키패드 탐색 시도')
            driver.switch_to.window(original_window)
        else:
            step('결제 창으로 전환 완료')

        # 키패드 대기
        step('PIN 키패드 대기 중')
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '#mtk_pinCodeInput'))
            )
            step('PIN 키패드(#mtk_pinCodeInput) 감지됨')
        except TimeoutException:
            step('PIN 키패드 DOM을 찾지 못했습니다')
            # 원래 창으로 복귀
            try:
                driver.switch_to.window(original_window)
            except Exception:
                pass
            return True

        time.sleep(0.08)
        # 숫자 스캔
        digit_map = {}
        buttons = driver.find_elements(By.CSS_SELECTOR, '#mtk_pinCodeInput a[role="button"][aria-label]')
        step(f'키패드 버튼 후보 {len(buttons)}개')
        for btn in buttons:
            try:
                label = (btn.get_attribute('aria-label') or '').strip()
                if label.isdigit() and btn.is_displayed():
                    digit_map[label] = btn
                    r = btn.rect
                    step(f'  숫자 {label} 위치 x={int(r.get("x",0))} y={int(r.get("y",0))}')
            except Exception:
                continue

        step(f'인식된 숫자 {len(digit_map)}개: {sorted(digit_map.keys())}')

        # 위치 기준으로 격자 배치 (1-1 ~ 4-2)
        items = []
        for label, btn in digit_map.items():
            try:
                r = btn.rect
                items.append((label, r.get('y', 0), r.get('x', 0)))
            except Exception:
                continue
        # y(행) → x(열) 순 정렬
        items.sort(key=lambda t: (round(t[1] / 30), t[2]))  # y를 대략적인 행으로 묶음

        # 행 그룹화
        rows = []
        current_row = []
        last_y = None
        for label, y, x in items:
            if last_y is None or abs(y - last_y) < 40:
                current_row.append((label, x))
                last_y = y if last_y is None else last_y
            else:
                if current_row:
                    rows.append(sorted(current_row, key=lambda t: t[1]))
                current_row = [(label, x)]
                last_y = y
        if current_row:
            rows.append(sorted(current_row, key=lambda t: t[1]))

        step('=== 키패드 격자 인식 결과 ===')
        grid_names = [
            ['1-1', '1-2', '1-3'],
            ['2-1', '2-2', '2-3'],
            ['3-1', '3-2', '3-3'],
            ['빈칸', '4-2', '빈칸'],
        ]
        for ri, row in enumerate(rows):
            names = grid_names[ri] if ri < len(grid_names) else [f'r{ri+1}-{i+1}' for i in range(len(row))]
            parts = []
            for ci, (label, x) in enumerate(row):
                name = names[ci] if ci < len(names) else f'?-{ci+1}'
                parts.append(f'{name}={label}')
            step('  ' + ' / '.join(parts))
        step('=== 격자 인식 종료 ===')

        # PIN 읽기: settings.json 우선
        import os, json
        pin = ''
        try:
            candidates = [
                Path(__file__).resolve().parent / 'settings.json',
                Path.cwd() / 'settings.json',
            ]
            for p in candidates:
                if p.exists():
                    data = json.loads(p.read_text(encoding='utf-8'))
                    val = data.get('NOL_MON_PIN', '')
                    pin = str(val).strip()
                    if pin:
                        step('settings.json에서 NOL_MON_PIN 읽음')
                        break
        except Exception as e:
            step(f'settings.json 읽기 실패: {type(e).__name__}')

        if not (pin.isdigit() and len(pin) == 6):
            pin = os.environ.get('NOL_MON_PIN', '').strip()
            if pin.isdigit() and len(pin) == 6:
                step('환경변수 NOL_MON_PIN 사용')
            else:
                step('PIN 미설정 · 입력 생략')
                step('키패드 창에 그대로 유지하고 종료')
                return True

        step('PIN 입력 시작')
        for i, ch in enumerate(pin, 1):
            if ch not in digit_map:
                step(f'  {i}/6 자리 숫자 없음')
                continue
            try:
                click(digit_map[ch])
                step(f'  입력 진행 {i}/6')
                time.sleep(0.14)
            except Exception as e:
                step(f'  {i}/6 클릭 실패: {type(e).__name__}')

        step('PIN 6자리 클릭 완료')
        time.sleep(0.3)

        # 확인 버튼 클릭 (실제 결제 진행)
        step('확인 버튼 대기')
        try:
            confirm = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '#mtk_pinCodeInput a[aria-label="확인"], a[aria-label="확인"]'))
            )
            click(confirm)
            step('확인 버튼 클릭 완료 · 결제 진행')
        except TimeoutException:
            step('확인 버튼을 찾지 못했습니다')
            step('키패드 창에 그대로 유지하고 종료')
            return True

        time.sleep(1.2)
        # 예약 완료 화면 확인
        step('예약 완료 화면 확인 중')
        success = False
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, '//*[contains(text(), "예약이 완료되었어요") or contains(text(), "예약이 완료")]'))
            )
            success = True
            step('예약 완료 인식: 예약이 완료되었어요')
        except TimeoutException:
            step('완료 문구를 찾지 못함 · 화면 텍스트 확인')

        # 주문번호 등 추가 정보 추출 시도
        try:
            body_text = driver.find_element(By.TAG_NAME, 'body').text
            for line in body_text.splitlines():
                line = line.strip()
                if any(k in line for k in ('주문번호', 'Y26', '통합 주문', '결제 금액', 'NOL 머니')):
                    step(f'  결과: {line[:80]}')
        except Exception:
            pass

        if success:
            step('테스트 결제 성공 · 예약 완료')
        else:
            step('테스트 결제 플로우 종료 (완료 화면 미확인)')
        return True
    except BookingTestError:
        raise
    except TimeoutException:
        raise BookingTestError(stage+' 단계의 화면 요소를 찾지 못했습니다') from None
    except Exception as error:
        # 원본 Selenium 오류/URL/응답 본문을 파일 로그에 남기지 않는다.
        raise BookingTestError(stage+' 실패 · '+type(error).__name__) from None


# === 4. 결제 준비·관리 화면 ===

OPTIONS={
    'method':('브라우저에서 직접 선택','Npay','신용/체크카드','기타'),
    'kind':('입장권','입장권 + 모노레일','모노레일 추가구매'),
    'quantity':('현재 목표 수량','1장 부분 가용'),
    'stage':('결제 직전','전체 결제 (PIN까지)'),
}


def scroll_page(parent):
    frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
    canvas=tk.Canvas(frame,bg=BG,highlightthickness=0)
    frame.scroll_canvas=canvas
    bar=ttk.Scrollbar(frame,orient='vertical',command=canvas.yview);bar.pack(side='right',fill='y')
    canvas.pack(side='left',fill='both',expand=True);canvas.configure(yscrollcommand=bar.set)
    body=ttk.Frame(canvas,padding=(0,0,8,12));window=canvas.create_window((0,0),window=body,anchor='nw')
    body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
    return body


def title(parent,text,subtitle=''):
    ttk.Label(parent,text=text,style='Section.TLabel').pack(anchor='w')
    if subtitle:
        ttk.Label(parent,text=subtitle,style='Muted.Card.TLabel',wraplength=800).pack(anchor='w',pady=(5,12))


class OperationsUI:
    def __init__(self,app):
        self.app=app
        self.path=app.base/'operation_settings.json'
        self.admin_enabled=False;self.rehearsing=False;self.index=0;self.steps=[]
        self.admin_widgets=[]
        values=self._load()
        self.vars={key:tk.StringVar(value=values[key]) for key in OPTIONS}
        self.target=tk.StringVar()
        self.setup_status=tk.StringVar()
        self.preview_status=tk.StringVar(value='시나리오를 고른 뒤 GUI 테스트를 시작하세요.')
        self.payment_status=tk.StringVar(value='결제수단과 인증은 실제 브라우저 결제창에서 직접 선택합니다.')
        self.admin_status=tk.StringVar(value='관리 모드 닫힘 · 열기를 누르면 화면 테스트 도구가 활성화됩니다.')
        self._payment(app.pages['payment'])
        self._admin(app.pages['admin'])
        self.refresh_context();self._buttons()

    def _load(self):
        try:
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw,dict): raw={}
        except (OSError,ValueError): raw={}
        return {key:raw[key] if raw.get(key) in choices else choices[0] for key,choices in OPTIONS.items()}

    def save(self):
        from storage import atomic_write, sanitize
        values={key:value.get() for key,value in self.vars.items()}
        if any(values[key] not in choices for key,choices in OPTIONS.items()):
            self.payment_status.set('목록에서 결제 준비 옵션을 선택하세요.')
            self.app._log('[OPERATIONS_SETTINGS_INVALID] 결제 준비 설정 확인 필요 · 목록에서 옵션을 선택하세요.')
            return
        try: atomic_write(self.path,json.dumps(values,ensure_ascii=False,indent=2))
        except OSError as error:
            self.payment_status.set('저장 실패 · '+sanitize(error))
            self.app._log('[OPERATIONS_SETTINGS_SAVE_FAILED] 결제 준비 설정 저장 실패 · '+sanitize(error))
            return
        self.payment_status.set('결제 준비·테스트 시나리오 저장 완료')
        self.app._log('결제 GUI 설정 저장 · 결제수단 선호 및 테스트 시나리오')

    def _payment(self,parent):
        body=scroll_page(parent)
        intro=card(body);intro.pack(fill='x',pady=(0,12))
        title(intro.content,'결제 준비','선택한 티켓을 확인하고 브라우저에서 결제를 직접 진행하는 작업 공간입니다.')
        ttk.Label(intro.content,textvariable=self.target,style='Card.TLabel',wraplength=850).pack(anchor='w',pady=(0,14))
        row=ttk.Frame(intro.content,style='Card.TFrame');row.pack(fill='x')
        ttk.Button(row,text='현재 선택 다시 가져오기',command=self.refresh_context).pack(side='left',padx=(0,8))
        ttk.Button(row,text='상품 페이지 열기 ↗',command=self.app._open_product).pack(side='left')
        form=card(body);form.pack(fill='x',pady=(0,12))
        title(form.content,'수동 결제 설정','실제 결제창에서 제공되는 수단을 직접 선택하세요. 아래 선택은 준비용 설정입니다.')
        row=ttk.Frame(form.content,style='Card.TFrame');row.pack(fill='x',pady=(0,12))
        ttk.Label(row,text='선호 결제수단',style='Card.TLabel').pack(side='left',padx=(0,12))
        ttk.Combobox(row,textvariable=self.vars['method'],values=OPTIONS['method'],state='readonly',width=25).pack(side='left')
        ttk.Label(form.content,text='카드정보 · 본인인증 · PIN',style='Card.TLabel').pack(anchor='w')
        ttk.Label(form.content,text='실제 브라우저 결제창에서 직접 입력',style='Muted.Card.TLabel').pack(anchor='w',pady=(5,14))
        self.save_button=ttk.Button(form.content,text='준비 설정 저장',command=self.save);self.save_button.pack(anchor='w')
        ttk.Label(form.content,textvariable=self.payment_status,style='Muted.Card.TLabel',wraplength=800).pack(anchor='w',pady=(10,0))
        checklist=card(body);checklist.pack(fill='x')
        title(checklist.content,'결제 전 직접 확인','확인란은 개인 점검용이며 실제 주문·결제 상태를 조회하지 않습니다.')
        self.checks=[]
        for text in ('상품 · 이용 날짜 · 입장 회차 확인','성인 · 경로 · 어린이 수량과 최종 금액 확인',
                     '모노레일 구간 · 탑승 시각 확인','할인 · 취소 조건 · 최종 결제 내용 확인'):
            value=tk.BooleanVar();self.checks.append(value)
            ttk.Checkbutton(checklist.content,text=text,variable=value).pack(anchor='w',pady=3)
        ttk.Button(checklist.content,text='관리 모드에서 테스트 준비 →',command=lambda:self.app._show_page('admin')).pack(anchor='w',pady=(12,0))

    def _admin(self,parent):
        body=scroll_page(parent)
        header=card(body);header.pack(fill='x',pady=(0,12))
        title(header.content,'관리 모드','초기셋업 점검과 결제 단계 GUI 테스트를 한곳에서 준비합니다.')
        row=ttk.Frame(header.content,style='Card.TFrame');row.pack(fill='x')
        self.unlock_button=ttk.Button(row,text='관리 모드 열기',command=self.toggle_admin);self.unlock_button.pack(side='left',padx=(0,12))
        ttk.Label(row,textvariable=self.admin_status,style='Muted.Card.TLabel',wraplength=650).pack(side='left')
        setup=card(body);setup.pack(fill='x',pady=(0,12))
        title(setup.content,'초기셋업 점검')
        ttk.Label(setup.content,textvariable=self.setup_status,style='Card.TLabel',wraplength=850).pack(anchor='w',pady=(8,12))
        row=ttk.Frame(setup.content,style='Card.TFrame');row.pack(fill='x')
        for text,command in (('초기셋업 점검',self.refresh_context),('로그인 · 세션 설정',lambda:self.app._show_page('settings')),('로그 폴더 열기',self.open_logs)):
            button=ttk.Button(row,text=text,command=command);button.pack(side='left',padx=(0,8));self.admin_widgets.append(button)
        scenario=card(body);scenario.pack(fill='x',pady=(0,12))
        title(scenario.content,'테스트 시나리오','티켓 종류·수량·결제 단계를 골라 확인할 화면 순서를 점검하세요. 테스트 수행은 이 화면에서만 진행합니다.')
        row=ttk.Frame(scenario.content,style='Card.TFrame');row.pack(fill='x')
        for index,(key,label) in enumerate((('kind','티켓 종류'),('quantity','수량 시나리오'),('stage','확인할 결제 단계'))):
            cell=ttk.Frame(row,style='Card.TFrame');cell.grid(row=0,column=index,sticky='ew',padx=(0,12));row.columnconfigure(index,weight=1)
            ttk.Label(cell,text=label,style='Muted.Card.TLabel').pack(anchor='w',pady=(0,5))
            widget=ttk.Combobox(cell,textvariable=self.vars[key],values=OPTIONS[key],state='readonly',width=22)
            widget.pack(fill='x');self.admin_widgets.append(widget)
        row=ttk.Frame(scenario.content,style='Card.TFrame');row.pack(fill='x',pady=(14,0))
        self.run_button=ttk.Button(row,text='테스트 수행 (GUI)',style='Accent.TButton',command=self.start_preview);self.run_button.pack(side='left',padx=(0,8))
        self.next_button=ttk.Button(row,text='다음 단계 확인',command=self.next_step);self.next_button.pack(side='left',padx=(0,8))
        self.stop_button=ttk.Button(row,text='테스트 종료',command=self.stop_preview);self.stop_button.pack(side='left',padx=(0,8))
        button=ttk.Button(row,text='시나리오 저장',command=self.save);button.pack(side='left');self.admin_widgets.append(button)
        # 로그인된 브라우저의 결제 직전 동선 테스트
        test_pay_row=ttk.Frame(scenario.content,style='Card.TFrame');test_pay_row.pack(fill='x',pady=(12,0))
        self.test_pay_button=ttk.Button(test_pay_row,text='테스트 결제 (브라우저)',style='Accent.TButton',command=self.start_test_booking)
        self.test_pay_button.pack(side='left',padx=(0,8))
        self.test_pay_status=tk.StringVar(value='첫 시각·첫 권종만 테스트 · 날짜·수량은 브라우저에서 먼저 선택 · 최종 결제 미실행')
        ttk.Label(test_pay_row,textvariable=self.test_pay_status,style='Muted.Card.TLabel',wraplength=700).pack(side='left')
        self.admin_widgets.append(self.test_pay_button)
        preview=card(body);preview.pack(fill='x')
        title(preview.content,'결제 동선 미리보기','실제 결제 테스트는 사용자가 브라우저에서 직접 진행합니다.')
        self.flow=ttk.Treeview(preview.content,columns=('step','state'),show='headings',height=6,selectmode='none')
        self.flow.heading('step',text='확인할 화면');self.flow.heading('state',text='GUI 확인 상태')
        self.flow.column('step',width=550);self.flow.column('state',width=180)
        self.flow.pack(fill='x')
        ttk.Label(preview.content,textvariable=self.preview_status,style='Card.TLabel',wraplength=850).pack(anchor='w',pady=(12,0))

    def refresh_context(self):
        app=self.app
        counts=' · '.join(f'{label} {app.counts[age].get()}장' for age,label in AGE_NAMES.items() if app.counts[age].get() not in ('','0'))
        lines=[app.product_title.get(),app.vars['date'].get()+'  |  '+counts]
        if app.vars['mode'].get()=='addon':
            lines.append('모노레일 추가구매 · '+app.vars['section'].get()+' · '+(app.vars['times'].get() or '회차 미선택'))
        else:
            lines.append('입장: '+(app.vars['times'].get() or '회차 미선택'))
            if app.vars['include_monorail'].get():
                timing='입장 +20분 ±15분' if app.vars['mono_relative'].get() else app.vars['mono_times'].get() or '가까운 후보 확인'
                lines.append('모노레일: '+app.vars['section'].get()+' · '+timing)
        self.target.set('\n'.join(lines))
        saved=sum(app.catalogue.summary(pid)[0] for pid in app.product_stats)
        session='저장 파일 있음 · 실제 로그인은 연결 시 확인' if (app.base/'session_cookies.json').is_file() else '로그인 세션 저장 필요'
        telegram='입력됨 · 전송 여부는 알림 테스트로 확인' if app.vars['token'].get().strip() and app.vars['chat'].get().strip() else '봇 토큰 · 채팅 ID 설정 필요'
        self.setup_status.set(f'NOL 세션: {session}\n상품 목록: {saved}일 저장  |  텔레그램: {telegram}')

    def apply_admin_defaults(self):
        if self.rehearsing: return
        for key in ('quantity','stage'):
            self.vars[key].set(OPTIONS[key][1])

    def toggle_admin(self):
        self.admin_enabled=not self.admin_enabled
        if self.admin_enabled: self.apply_admin_defaults()
        if not self.admin_enabled and self.rehearsing: self.stop_preview()
        self.unlock_button.configure(text='관리 모드 닫기' if self.admin_enabled else '관리 모드 열기')
        self.admin_status.set('관리 도구 활성화 · 로컬 화면 테스트' if self.admin_enabled else '관리 모드 닫힘')
        self.app._log('[ADMIN_MODE_OPENED] 관리 모드 열림' if self.admin_enabled else '[ADMIN_MODE_CLOSED] 관리 모드 닫힘')
        self._buttons()

    def _buttons(self):
        working=self.app.busy or self.app.runner.running
        for widget in self.admin_widgets:
            enabled=self.admin_enabled and not self.rehearsing and not working
            widget.configure(state=('readonly' if isinstance(widget,ttk.Combobox) else 'normal') if enabled else 'disabled')
        self.run_button.configure(state='normal' if self.admin_enabled and not self.rehearsing and not working else 'disabled')
        for widget in (self.next_button,self.stop_button): widget.configure(state='normal' if self.rehearsing else 'disabled')

    def start_test_booking(self):
        """첫 시각·첫 권종의 브라우저 동선을 공유 작업 큐에서 시험한다."""
        from storage import sanitize
        app=self.app
        if not self.admin_enabled:
            app._log('관리 모드를 먼저 열어주세요');return
        if app.closed or app.busy or app.session_pending or app.runner.running or self.rehearsing:
            app._log('다른 작업이 실행 중입니다');return
        try:
            config=app.read_config()
            if config.mode!='entry': raise ValueError('브라우저 테스트는 입장권 모드만 지원합니다')
        except ValueError as error:
            app._log('예약 테스트 설정 오류 · '+sanitize(error))
            self.test_pay_status.set('설정 확인 필요');return
        age_label=AGE_NAMES[config.selected_ages[0]]
        self.test_pay_status.set('브라우저 테스트 진행 중 · 최종 결제 미실행')
        app.test_booking_hold = True
        app._log(f'[TEST-BOOKING] 시작 · 첫 시각 {config.time_specs[0].split("~")[0]} · 권종 {age_label} · 날짜·수량은 브라우저 선택값')
        app._lock_conditions(True)

        def job():
            browser=app.browser
            try:
                with app.client.lock, browser.lock:
                    if app.closed: raise BookingTestError('프로그램 종료 요청으로 예약 테스트를 중지했습니다')
                    if browser.driver is None:
                        raise BookingTestError('브라우저가 연결되지 않았습니다. 먼저 로그인 브라우저를 열어주세요')
                    browser._select()
                    actual=(browser.driver.current_url or '').split('?')[0].rstrip('/')
                    expected=config.url.split('?')[0].rstrip('/')
                    if actual!=expected:
                        raise BookingTestError('대상 상품 페이지에서 날짜와 수량을 먼저 선택하세요')
                    browser.headers={};browser.ready_url=None
                    return run_test_booking(browser.driver,config,age_label=age_label,stop_before_pay=True,
                        should_stop=lambda:app.closed,
                        log=lambda message:app._emit({'kind':'log','message':message}))
            except BookingTestError: raise
            except Exception as error:
                raise BookingTestError('브라우저 테스트 실패 · '+type(error).__name__) from None

        app._submit(job,'test_pay_done')
        # 테스트 끝난 뒤 30초간 keepalive가 페이지를 건드리지 못하게 함
        app.root.after(30000, lambda: setattr(app, 'test_booking_hold', False))
    def _start_auto_booking(self, snapshot=None):
        """감시 중 가용 발견 시 자동으로 예약(결제) 시도."""
        if self.closed or self.busy or self.runner.running is False:
            return
        try:
            config = self.read_config()
            if config.mode != 'entry':
                self._log('자동 예약은 입장권 모드만 지원'); return
        except Exception as e:
            self._log('자동 예약 설정 오류 · ' + str(e)); return

        age_label = AGE_NAMES.get(config.selected_ages[0], '성인') if config.selected_ages else '성인'
        self.test_booking_hold = True
        self._log(f'[AUTO-BOOKING] 가용 발견 · {age_label} 예약 시도')
        self._lock_conditions(True)

        def job():
            browser = self.browser
            try:
                with self.client.lock, browser.lock:
                    if self.closed:
                        raise BookingTestError('종료 요청')
                    if browser.driver is None:
                        raise BookingTestError('브라우저 미연결')
                    browser._select()
                    return run_test_booking(
                        browser.driver, config,
                        age_label=age_label,
                        stop_before_pay=False,  # 실제 결제까지
                        should_stop=lambda: self.closed,
                        log=lambda m: self._emit({'kind': 'log', 'message': m})
                    )
            except BookingTestError as e:
                self._emit({'kind': 'log', 'message': f'[AUTO-BOOKING] 실패 · {e}'})
                return False
            except Exception as e:
                self._emit({'kind': 'log', 'message': f'[AUTO-BOOKING] 오류 · {type(e).__name__}'})
                return False
            finally:
                # 결제 후 상품 페이지로 복귀 시도
                try:
                    if browser.driver:
                        browser.driver.get(config.url)
                        self._emit({'kind': 'log', 'message': '[AUTO-BOOKING] 상품 페이지 복귀'})
                except Exception:
                    pass
                self.test_booking_hold = False
                self._emit({'kind': 'log', 'message': '[AUTO-BOOKING] 완료 · 감시 계속'})

        self._submit(job, 'auto_booking_done')



    def start_preview(self):
        if not self.admin_enabled or self.rehearsing or self.app.busy or self.app.runner.running: return
        self.refresh_context()
        self.steps=['상품 · 날짜 · 권종 수량 확인']
        if self.vars['kind'].get()!='입장권': self.steps.append('모노레일 구간 · 탑승 시각 확인')
        self.steps.extend(['주문 내용 · 최종 금액 확인','결제수단 선택 · 결제 직전'])
        if self.vars['stage'].get()=='전체 결제 (PIN까지)':
            self.steps.extend(['약관 · 결제 내용 확인','본인인증 · PIN 직접 입력','브라우저에서 결제 결과 확인'])
        self.flow.delete(*self.flow.get_children())
        for index,step in enumerate(self.steps): self.flow.insert('','end',iid=str(index),values=(step,'확인 중' if index==0 else '대기'))
        self.index=0;self.rehearsing=True
        self.preview_status.set('GUI 점검 중 · '+self.vars['kind'].get()+' / '+self.vars['quantity'].get()+' / '+self.vars['stage'].get())
        self.app._log('관리모드 GUI 점검 시작 · 브라우저 동작 없음');self._buttons()

    def next_step(self):
        if not self.rehearsing: return
        self.flow.item(str(self.index),values=(self.steps[self.index],'UI 확인'))
        self.index+=1
        if self.index==len(self.steps):
            self.rehearsing=False;self.preview_status.set('화면 점검 완료 · 실제 결제 테스트는 브라우저에서 직접 진행하세요.')
            self.app._log('관리모드 화면 점검 완료')
        else:
            self.flow.item(str(self.index),values=(self.steps[self.index],'확인 중'));self.flow.see(str(self.index))
            self.preview_status.set('GUI 점검 중 · '+self.steps[self.index])
        self._buttons()

    def stop_preview(self):
        if not self.rehearsing: return
        self.flow.item(str(self.index),values=(self.steps[self.index],'중지'))
        self.rehearsing=False;self.preview_status.set('GUI 테스트 종료 · 외부 주문·결제 상태와 무관합니다.')
        self.app._log('관리모드 GUI 점검 종료');self._buttons()

    def open_logs(self):
        try: os.startfile(str(self.app.base/'logs'))
        except OSError as error: self.app._problem(error)


# === 5. 설정 변환·앱 제어 ===

class QueryGuard:
    def __init__(self): self.revision=0
    def changed(self): self.revision+=1
    def ticket(self): return self.revision
    def accepts(self,ticket): return ticket==self.revision


def config_from_form(form):
    try:
        return WatchConfig(mode=form['mode'],product_url=form['product_url'].strip(),addon_url=form.get('addon_url','').strip(),
            date=form['date'].strip()[:10],time_specs=parse_time_specs(form['times']),
            counts={age:int(form['counts'].get(age,0)) for age in AGE_NAMES},
            section=form.get('section','MONORAIL_SEGMENT_2'),include_monorail=bool(form.get('include_monorail',False)),
            mono_specs=parse_time_specs(form.get('mono_times','')),mono_relative=bool(form.get('mono_relative',False)),interval=float(form.get('interval',2)),
            repeat_seconds=float(form.get('repeat',0))).validate()
    except (ValueError,TypeError) as error:
        raise ValueError('설정을 확인하세요: '+str(error)) from None


class App:
    def __init__(self,root,base_dir,*,auto_open_browser=True):
        from api import LeisureClient
        from browser import BrowserSession
        from catalog import Catalogue, product_id
        from logfiles import create_app_logger
        from storage import SettingsStore, load_telegram_secrets
        self.root=root
        self.base=Path(base_dir)
        self.closed=False
        self.busy=False
        self.session_pending=False
        self.test_booking_hold=False
        self.syncing=False
        self.catalog_stop=threading.Event()
        self.mutating=False
        self.events=queue.Queue()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hwadam-query')
        self.pending_jobs=set()
        self.jobs_lock=threading.Lock()
        self._event_persisted=False
        self._status_keys={}
        self.guard=QueryGuard()
        self.store=SettingsStore(self.base)
        config,warning=self.store.load()
        self.catalogue=Catalogue(self.base)
        self.choices_file=self.base/'browse_choices.json'
        try:
            self.choices=json.loads(self.choices_file.read_text(encoding='utf-8'))
            if not isinstance(self.choices.get('times'),dict) or not isinstance(self.choices.get('dates'),dict): raise ValueError()
            self.choices['times']={key:value for key,value in self.choices['times'].items()
                if isinstance(value,dict) and all(isinstance(value.get(field,''),str) for field in ('times','mono_times'))}
        except (OSError,ValueError,TypeError,AttributeError): self.choices={'times':{},'dates':{}}
        token,chat=load_telegram_secrets(self.base)
        self.vars={
            'mode':tk.StringVar(value=config.mode),'product_url':tk.StringVar(value=config.product_url),
            'addon_url':tk.StringVar(value=config.addon_url),'date':tk.StringVar(value=date_label(config.date)),
            'times':tk.StringVar(value=', '.join(config.time_specs)),'section':tk.StringVar(value=SECTION_NAMES[config.section]),
            'include_monorail':tk.BooleanVar(value=config.include_monorail),'mono_times':tk.StringVar(value=', '.join(config.mono_specs)),
            'mono_relative':tk.BooleanVar(value=config.mono_relative),
            'interval':tk.StringVar(value=f'{config.interval:g}'),'repeat':tk.StringVar(value=f'{config.repeat_seconds:g}'),
            'token':tk.StringVar(value=token),'chat':tk.StringVar(value=chat)}
        self.counts={age:tk.StringVar(value=str(config.counts.get(age,0))) for age in AGE_NAMES}
        self.primary_times=[]
        self.secondary_times=[]
        self.condition_widgets=[]
        self.widget_states={}
        self.status=tk.StringVar(value='대기 · 조건을 확인하고 브라우저를 연결하세요')
        self.session_status=tk.StringVar(value='저장된 세션 있음 · 연결 시 자동 확인' if (self.base/'session_cookies.json').is_file() else '로그인 세션 미저장 · 로그인 후 세션 저장하세요')
        self.detail=tk.StringVar(value='성인 2 · 경로 2 · 어린이 2 | 가용 1장부터 알림')
        self.primary_title=tk.StringVar(value='입장 회차 · 여러 개 선택 가능')
        self.product_title=tk.StringVar(value=(self.catalogue.product(product_id(config.product_url)) or {}).get('name','화담숲 입장권'))
        self.catalog_stamp=tk.StringVar(value='저장된 회차 목록을 불러오는 중')
        self.mono_stamp=tk.StringVar(value='선택 날짜의 모노레일 목록을 불러오는 중')
        self.date_summary=tk.StringVar(value=date_label(config.date))
        self.selection_count=tk.StringVar(value='선택한 회차')
        self.selection_summary=tk.StringVar(value='')
        self.order_label=tk.StringVar(value='추가구매는 입장권 주문에서 연 모노레일 주소를 붙여넣으세요.')
        self.logger,self.log_handler=create_app_logger(self.base)
        self.root.report_callback_exception=self._callback_exception
        self.logger.info('프로그램 시작 · GUI',extra={'event':'APP_STARTED'})
        for variable,code in ((self.status,'UI_STATUS'),(self.session_status,'SESSION_STATUS')):
            variable.trace_add('write',lambda *_,value=variable,event_code=code:self._status_changed(value,event_code))
        self.browser=BrowserSession(self.base,lambda text:self._emit({'kind':'log','message':text}))
        self.client=LeisureClient(self.browser)
        self.runner=MonitorRunner(self.client,self._emit)
        self.browse_key=self._choice_key()
        previous=self.choices['times'].get(self.browse_key)
        if previous:
            self.vars['times'].set(previous.get('times',''))
            self.vars['mono_times'].set(previous.get('mono_times',''))
        self._build()
        for key,value in self.vars.items():
            if key not in ('token','chat'):
                value.trace_add('write',lambda *_,field=key:self._condition_changed(field))
        for value in self.counts.values(): value.trace_add('write',lambda *_:self._condition_changed('counts'))
        self._mode_changed()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.poll_id=self.root.after(100,self._poll)
        self.keepalive_id=self.root.after(30000,self._keep_session_alive)
        self._log(warning or self.catalogue.warning or '준비 완료 · 상품을 선택하고 날짜와 회차를 체크하세요')
        # .vbs / 일반 실행 시 브라우저 자동 시작 (세션이 있으면 재사용)
        self.browser_open_id=self.root.after(1500,self._auto_open_browser) if auto_open_browser else None

    def _auto_open_browser(self):
        """프로그램 시작 시 로그인 브라우저를 자동으로 연다."""
        self.browser_open_id=None
        if self.closed or self.busy or self.runner.running:
            return
        try:
            url = self._session_url()
        except Exception:
            url = DEFAULT_URL
        self._log('시작 시 브라우저 자동 연결 시도')
        self.status.set('시작 시 브라우저 자동 연결 중…')
        self._submit(lambda: self.browser.open_login(url), 'login_opened')

    def _show_page(self,name):
        if name in ('payment','admin') and hasattr(self,'operations'):
            if name=='admin': self.operations.apply_admin_defaults()
            self.operations.refresh_context()
        for key,page in self.pages.items():
            if key==name: page.grid()
            else: page.grid_remove()
        for key,button in self.nav_buttons.items():
            button.configure(bg='#315642' if key==name else '#173A30')

    def _choice_key(self):
        from catalog import product_id
        source=product_id(self.vars['product_url'].get())
        if self.vars['mode'].get()=='addon':
            source=hashlib.sha256(self.vars['addon_url'].get().strip().encode('utf-8')).hexdigest()
        return '|'.join((source,self.vars['mode'].get(),
                         self.vars['date'].get()[:10],self.vars['section'].get()))

    def _remember(self):
        from catalog import product_id
        self.choices['times'][self.browse_key]={'times':self.vars['times'].get(),'mono_times':self.vars['mono_times'].get()}
        self.choices['dates'][product_id(self.vars['product_url'].get())]=self.vars['date'].get()[:10]

    def _condition_changed(self,key):
        self.guard.changed()
        if self.mutating: return
        if key in ('product_url','date','mode','section','addon_url'):
            self._remember()
            self.browse_key=self._choice_key()
            previous=self.choices['times'].get(self.browse_key,{})
            self.mutating=True
            if key!='section' or self.vars['mode'].get()!='entry':
                self.vars['times'].set(previous.get('times',''))
            self.vars['mono_times'].set(previous.get('mono_times',''))
            self.mutating=False
            self._clear_times()
            self._cached_view()
        else:
            self._remember()
            if key=='mono_relative':
                if self.vars['mono_relative'].get(): self.vars['include_monorail'].set(True)
                self._mode_changed(clear=False)
            if key in ('times','mono_times','mono_relative'):
                self._sync_checks()
        self._update_summary()

    def _choose_product(self,pid):
        if self.runner.running: return
        self._remember()
        product=self.catalogue.product(pid)
        if not product: return
        date=self.choices['dates'].get(pid) or product.get('default_date') or product['start']
        if not isinstance(date,str) or not product['start']<=date<=product['end']: date=product['start']
        self.mutating=True
        self.vars['product_url'].set(product['url']);self.vars['date'].set(date_label(date))
        self.browse_key=self._choice_key()
        previous=self.choices['times'].get(self.browse_key,{})
        self.vars['times'].set(previous.get('times',''))
        self.vars['mono_times'].set(previous.get('mono_times',''))
        self.mutating=False
        self._clear_times();self._cached_view();self._update_summary();self._show_page('explore')

    def _update_product_stats(self):
        from catalog import product_id
        current=product_id(self.vars['product_url'].get())
        for pid,var in self.product_stats.items():
            saved,total=self.catalogue.summary(pid)
            var.set(f'{saved}일 회차 저장'+(f' / 예약 {total}일' if total else '') if saved else '날짜별 회차 목록 미확보')
            self.product_buttons[pid].configure(bg='#3C634A' if pid==current else '#214638')

    def _cached_view(self):
        from catalog import product_id
        pid=product_id(self.vars['product_url'].get());date=self.vars['date'].get()[:10]
        product=self.catalogue.product(pid) or {'name':'화담숲 입장권','start':'','end':'','dates':[],'days':{}}
        self.product_title.set(product['name'])
        self.calendar.set_context(product,date)
        values=product.get('dates') or []
        self.date_combo.configure(values=[date_label(value) for value in values])
        self._update_product_stats()
        if self.vars['mode'].get()=='addon':
            self.catalog_stamp.set('주문 링크로 연결하면 추가구매 가능한 실제 회차를 표시합니다.')
            self._update_summary();return
        record=self.catalogue.rounds(pid,date)
        if record is not None:
            self._fill_list(self.primary_list,record['rows'],parse_time_specs(self.vars['times'].get()),'primary')
            stamp=record['observed_at'].replace('T',' ')[:16]
            self.catalog_stamp.set(f"저장된 목록 · {stamp} 확인 · {len(record['rows'])}회차\n현재 재고는 감시 시작 후 확인합니다.")
        else:
            self.catalog_stamp.set('회차 목록 미확보 · 「두 상품 목록 확보」 또는 「이 날짜 새로고침」을 눌러주세요.')
        section=next((code for code,label in SECTION_NAMES.items() if label==self.vars['section'].get()),'')
        mono=self.catalogue.rounds(pid,date,section=section) if section else None
        if mono is not None:
            self._fill_list(self.secondary_list,mono['rows'],parse_time_specs(self.vars['mono_times'].get()),'secondary')
            self.mono_stamp.set(f"{date_label(date)} · {len(mono['rows'])}회차\n{mono['parent_time'] or '당일 첫'} 입장 기준 저장 목록")
        else: self.mono_stamp.set('이 날짜의 모노레일 목록 미확보\n「이 날짜 새로고침」으로 읽어주세요.')
        self._sync_checks()
        self._update_summary()

    def _display_mono_specs(self):
        if self.vars['mono_relative'].get() and self.vars['mode'].get()=='entry':
            selected=[time for time in self.primary_times if time_matches(time,parse_time_specs(self.vars['times'].get()))]
            return tuple(spec for time in selected for spec in relative_monorail_specs(time))
        return parse_time_specs(self.vars['mono_times'].get())

    def _sync_checks(self):
        for widget,key in ((self.primary_list,'times'),(self.secondary_list,'mono_times')):
            specs=self._display_mono_specs() if key=='mono_times' else parse_time_specs(self.vars[key].get())
            widget.selected={time for time,_ in widget.items if time_matches(time,specs)}
            widget.render()

    def _update_summary(self):
        value=self.vars['date'].get()[:10]
        try: self.date_summary.set(date_label(value))
        except ValueError: self.date_summary.set('날짜를 선택하세요')
        selected=[time for time in self.primary_times if time_matches(time,parse_time_specs(self.vars['times'].get()))]
        mono=[time for time in self.secondary_times if time_matches(time,self._display_mono_specs())]
        def label(times,fallback):
            return (', '.join(times[:3])+(f' 외 {len(times)-3}개' if len(times)>3 else '')) if times else fallback
        text=label(selected,self.vars['times'].get() or '미선택')
        if self.vars['mode'].get()=='addon':
            self.selection_count.set(f'모노레일 {len(selected)}개 회차 선택')
            self.selection_summary.set(text)
        else:
            self.selection_count.set(f'입장 {len(selected)}개 · 모노레일 {len(mono)}개 선택')
            fallback='가까운 후보 확인' if self.vars['include_monorail'].get() else '미선택'
            mono_label='입장 +20분 ±15분' if self.vars['mono_relative'].get() else label(mono,fallback)
            self.selection_summary.set('입장: '+text+'\n모노레일: '+mono_label)

    def _sync_catalogues(self):
        from catalog import sync_catalogues
        if self.busy or self.runner.running: return
        self.syncing=True;self.catalog_stop.clear()
        self.status.set('두 상품의 날짜·회차 목록을 확보하는 중…')
        def job():
            with self.client.lock:
                return sync_catalogues(self.client,self.catalogue,self.catalog_stop,self._emit,resume=True)
        self._submit(job,'catalog_done')

    def _condition(self,widget):
        self.condition_widgets.append(widget)
        self.widget_states[widget]=str(widget.cget('state')) if 'state' in widget.keys() else 'normal'
        return widget

    def _entry(self,parent,key,**kwargs):
        widget=ttk.Entry(parent,textvariable=self.vars[key],**kwargs)
        self._condition(widget)
        return widget

    def _build(self):
        from ui_layout import build
        build(self,OperationsUI)

    def _wheel(self,event):
        widget=event.widget
        while widget is not None:
            scroller=getattr(widget,'scroll_canvas',None)
            if scroller is not None:
                scroller.yview_scroll(-int(event.delta/120),'units');return 'break'
            if isinstance(widget,TimeGrid):
                widget.scroll_units(-int(event.delta/120));return 'break'
            if widget==self.left_content or widget==self.canvas:
                self.canvas.yview_scroll(-int(event.delta/120),'units'); return 'break'
            widget=getattr(widget,'master',None)

    def read_config(self):
        values={key:value.get() for key,value in self.vars.items()}
        values['section']=next((code for code,label in SECTION_NAMES.items() if label==values['section']),'')
        values['counts']={age:value.get() for age,value in self.counts.items()}
        return config_from_form(values)

    def _mode_changed(self,clear=True):
        addon=self.vars['mode'].get()=='addon'
        self.primary_title.set('모노레일 추가구매 회차' if addon else '입장 회차')
        if not self.runner.running:
            self.addon_entry.configure(state='normal' if addon else 'disabled')
            self.mono_check.configure(state='disabled' if addon else 'normal')
            self.relative_check.configure(state='disabled' if addon else 'normal')
            enabled=not addon and not self.vars['mono_relative'].get()
            self.mono_entry.configure(state='normal' if enabled else 'disabled')
            self.secondary_list.configure(state='normal' if enabled else 'disabled')
            self.calendar.set_enabled(not addon)
        if addon:
            self.addon_card.pack(fill='x',pady=(0,12),before=self.date_card)
        else: self.addon_card.pack_forget()
        if addon:
            self.mono_card.grid_remove();self.primary_card.grid_configure(columnspan=2,padx=0)
        else:
            self.mono_card.grid();self.primary_card.grid_configure(columnspan=1,padx=(0,6))
        if clear: self._clear_times();self._cached_view()

    def _clear_times(self):
        for widget in (self.primary_list,self.secondary_list):
            state=widget.cget('state'); widget.configure(state='normal'); widget.delete(0,'end'); widget.configure(state=state)
        self.primary_times=[]; self.secondary_times=[]
        self.table.delete(*self.table.get_children())
        self.extra_label.set('조건 변경 후 회차 새로고침으로 실제 목록을 읽으세요.')

    def _quick_range(self):
        self.vars['times'].set('08:00~08:20')

    def _selected(self,kind):
        widget,times,key=(self.primary_list,self.primary_times,'times') if kind=='primary' else (self.secondary_list,self.secondary_times,'mono_times')
        if times:
            self.vars[key].set(', '.join(times[index] for index in widget.curselection()))
            if kind=='secondary' and widget.curselection(): self.vars['include_monorail'].set(True)

    @staticmethod
    def _round_label(row):
        status=row.get('roundStatusCode')
        return ('품절' if status=='SOLD_OUT' or row.get('inventoryQuantity')==0 else
                '판매 중' if status in ('IN_SALE','OPEN','AVAILABLE') else '판매 상태 확인 필요')

    def _emit(self,event):
        # Tk 큐가 지연되거나 창이 닫혀도 작업 결과는 발생 시점에 저장한다.
        from eventlog import persist_event
        persist_event(self.logger,event)
        if not self.closed: self.events.put({**event,'_persisted':True})

    def _callback_exception(self,error_type,error,tb):
        locations=';'.join(f'{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}'
                           for frame,line in traceback.walk_tb(tb))
        self.logger.error('화면 처리 오류 error_type=%s location=%s',error_type.__name__,locations,
                          extra={'event':'GUI_CALLBACK_FAILED'})

    def _background(self,work):
        future=self.executor.submit(work)
        with self.jobs_lock: self.pending_jobs.add(future)
        def completed(done):
            with self.jobs_lock: self.pending_jobs.discard(done)
        future.add_done_callback(completed)

    def _status_changed(self,variable,code):
        from storage import sanitize
        text=sanitize(variable.get())
        key='NOL 연결 정상' if text.startswith('NOL 연결 정상 · ') else text
        previous=self._status_keys.get(code)
        self._status_keys[code]=key
        if key!=previous and not self._event_persisted:
            self.logger.info(text,extra={'event':code})

    def _submit(self,job,kind,*,ticket=None):
        from api import AuthLost
        from storage import sanitize
        self.busy=True; self._buttons()
        def work():
            try: event={'kind':kind,'value':job(),'ticket':ticket}
            except Exception as error: event={'kind':'job_error','operation':kind,'message':sanitize(error),'ticket':ticket,'auth':isinstance(error,AuthLost)}
            self._emit(event)
        self._background(work)

    def _session_url(self):
        addon=self.vars['mode'].get()=='addon' and self.vars['addon_url'].get().strip()
        return validate_url(addon or self.vars['product_url'].get(),order=bool(addon))

    def _open_login(self):
        if self.busy or self.runner.running: return
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        self.status.set('전용 로그인 브라우저를 여는 중…')
        self._submit(lambda:self.browser.open_login(url),'login_opened')

    def _save_session(self):
        if self.busy or self.runner.running: return
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        self.status.set('로그인 확인 후 세션을 저장하는 중…')
        self._submit(lambda:self.browser.save_login_session(url),'session_saved')

    def _keep_session_alive(self):
        from api import AuthLost
        from storage import sanitize
        if self.closed: return
        self.root.after_cancel(self.keepalive_id)
        self.keepalive_id=self.root.after(60000,self._keep_session_alive)
        # 감시 루프는 자체 갱신을 수행한다. 로그인·탐색 작업 중에는 이동하지 않는다.
        if self.busy or self.runner.running or self.session_pending or getattr(self, "test_booking_hold", False): return
        try: url=validate_url(self.vars['product_url'].get())
        except ValueError: url=DEFAULT_URL
        self.session_pending=True
        def work():
            event={'kind':'session_maintenance','value':{'state':'busy'}}
            if not self.closed and self.client.lock.acquire(blocking=False):
                try:
                    if not self.closed:
                        event['value']=self.browser.maintain_session(url)
                except Exception as error:
                    event={'kind':'session_maintenance','message':sanitize(error),'auth':isinstance(error,AuthLost)}
                finally:
                    self.client.lock.release()
            self._emit(event)
        self._background(work)

    def _show_session_maintenance(self,event):
        self.session_pending=False
        self.test_booking_hold=False
        if 'message' in event:
            self.session_status.set('로그인 유지 중단 · 전용 Edge에서 다시 로그인하세요' if event.get('auth')
                                    else '로그인 유지 확인 지연 · 자동 재시도 대기')
            self._log('로그인 유지 실패 · '+event['message'])
            return
        result=event['value']
        if result['state'] not in ('checked','renewed'): return
        expiry=result.get('expires_at',0)
        suffix=('만료 예정 '+dt.datetime.fromtimestamp(expiry,dt.timezone(dt.timedelta(hours=9))).strftime('%m/%d %H:%M:%S')
                if expiry else '만료 시각 미공개')
        prefix='로그인 유지 확인' if result['state']=='renewed' else '세션 쿠키 점검 '+dt.datetime.now().strftime('%H:%M')
        self.session_status.set(prefix+' · '+suffix)

    def _browse_config(self):
        values={key:value.get() for key,value in self.vars.items()}
        values['section']=next((code for code,label in SECTION_NAMES.items() if label==values['section']),'')
        values['counts']=({age:value.get() for age,value in self.counts.items()} if values['mode']=='addon'
                          else {age:1 for age in AGE_NAMES})
        values.update(times='00:00~23:59',mono_times='',interval=2,repeat=0)
        return config_from_form(values)

    def _load(self,refresh):
        from api import AuthLost
        from storage import sanitize
        if self.busy or self.runner.running: return
        try: config=self._browse_config()
        except ValueError as error: self._problem(error); return
        self.guard.changed(); ticket=self.guard.ticket()
        self.status.set('상품·날짜·회차를 읽는 중…')
        def job():
            with self.client.lock:
                catalog=self.client.prepare(config,refresh=refresh)
                dates=self.client.schedules(config)
                mono=config.mode=='addon'
                parent=self.client._order.entry_time if mono else None
                primary=self.client.rounds(config,monorail=mono,entry_time=parent)
                secondary=[]; extra_error=''
                if not mono:
                    try:
                        parent=min(row['time'] for row in primary) if primary else None
                        if parent: secondary=self.client.rounds(config,monorail=True,entry_time=parent)
                    except AuthLost: raise
                    except Exception as error: extra_error=sanitize(error)
                if not mono:
                    self.catalogue.put_catalog(catalog,dates)
                    self.catalogue.put_rounds(catalog.id,config.date,primary)
                    if not extra_error:
                        self.catalogue.put_rounds(catalog.id,config.date,secondary,section=config.section,parent_time=parent or '',ages=config.selected_ages)
                return config,catalog,dates,primary,secondary,self.client._order,extra_error
        self._submit(job,'loaded',ticket=ticket)

    def _show_loaded(self,value):
        config,catalog,dates,primary,secondary,order,extra_error=value
        self.date_combo.configure(values=[date_label(row['date']) for row in dates if row.get('date')])
        self.product_title.set(catalog.name)
        self._fill_list(self.primary_list,primary,parse_time_specs(self.vars['times'].get()),'primary')
        self._fill_list(self.secondary_list,secondary,parse_time_specs(self.vars['mono_times'].get()),'secondary')
        self.status.set('연결 완료 · 감시 시작을 누르세요')
        self.session_status.set('로그인 확인 · 세션 자동 저장 완료')
        self.detail.set(catalog.name+' | '+date_label(config.date)+' | 회차 목록 조회 완료')
        self.order_label.set(('주문 확인: '+order.entry_time+' 입장 · '+count_label(order.counts)) if order else '추가구매는 입장권 주문에서 연 모노레일 주소를 붙여넣으세요.')
        matches=[row for row in primary if time_matches(row['time'],parse_time_specs(self.vars['times'].get()))]
        self.extra_label.set(extra_error or (f'목표 범위에 실제 {len(matches)}회차 · 권종 가용 여부는 감시 시작 후 확인' if matches else '목표 시간에 실제 회차가 없습니다. 범위 또는 날짜를 확인하세요.'))
        self._log(f'목록 읽기 완료 · 날짜 {len(dates)}개 · 회차 {len(primary)}개 · 목표 일치 {len(matches)}개')
        self.catalog_stamp.set(f"방금 조회한 목록 · {len(primary)}회차 · 품절 회차도 감시 대상으로 선택할 수 있습니다.")
        self.mono_stamp.set(extra_error or f'{date_label(config.date)} · {len(secondary)}회차\n당일 첫 입장 기준 · 방금 조회')
        product=self.catalogue.product(str(catalog.id))
        if product: self.calendar.set_context(product,config.date)
        self._sync_checks();self._update_product_stats();self._update_summary()

    def _fill_list(self,widget,rows,specs,kind):
        times=[row['time'] for row in rows]
        widget.set_rows(rows,[time for time in times if time_matches(time,specs)])
        if kind=='primary': self.primary_times=times
        else: self.secondary_times=times

    def _save(self,show=True):
        from storage import atomic_write, save_telegram_secrets
        try:
            config=self.read_config(); self.store.save(config)
            save_telegram_secrets(self.base,self.vars['token'].get(),self.vars['chat'].get())
            self._remember();atomic_write(self.choices_file,json.dumps(self.choices,ensure_ascii=False,indent=2))
            if show: self._log('설정 저장 완료 · hwadam_codex 폴더')
            return config
        except (ValueError,OSError) as error:
            self._problem(error); return None

    def _start(self):
        if self.busy or self.runner.running: return
        try:
            notifier=TelegramNotifier(self.vars['token'].get(),self.vars['chat'].get()).validate()
            config=self.read_config()
        except (ValueError,RuntimeError) as error: self._problem(error); return
        if self._save(False) is None: return
        self.guard.changed()
        self.status.set('감시 준비 중…')
        self.detail.set(date_label(config.date)+' | '+', '.join(config.time_specs)+' | '+count_label(config.counts))
        self.runner.start(config,notifier)
        self._lock_conditions(True); self._buttons()

    def _stop(self):
        if self.syncing:
            self.catalog_stop.set();self.status.set('목록 확보 중지 요청 · 이미 읽은 목록은 저장됩니다')
            self.stop_button.configure(state='disabled');return
        self.runner.stop(); self.status.set('중지 요청 · 진행 중인 조회가 끝나면 종료됩니다')
        self.stop_button.configure(state='disabled')

    def _lock_conditions(self,locked):
        for widget in self.condition_widgets:
            widget.configure(state='disabled' if locked else self.widget_states[widget])
        if not locked: self._mode_changed(clear=False)
        else: self.calendar.set_enabled(False)

    def _buttons(self):
        running=self.runner.running
        state='disabled' if self.busy or running else 'normal'
        for widget in (self.catalog_button,self.login_button,self.session_button,self.connect_button,self.refresh_button,self.start_button,self.test_button,self.save_button): widget.configure(state=state)
        can_stop=running and not self.runner.stop_event.is_set() or self.syncing and not self.catalog_stop.is_set()
        self.stop_button.configure(state='normal' if can_stop else 'disabled')
        if hasattr(self,'operations'): self.operations._buttons()

    def _test_telegram(self):
        if self.busy or self.runner.running: return
        notifier=TelegramNotifier(self.vars['token'].get(),self.vars['chat'].get())
        self._submit(lambda:notifier.send('🌳 hwadam_codex 알림 테스트\n성인·경로·어린이 중 선택 권종이 1장이라도 가능하면 알립니다.'),'tested')

    def _show_snapshot(self,event):
        snapshot=event['snapshot']; self.table.delete(*self.table.get_children())
        for row in (*snapshot.rounds,*snapshot.monorail):
            stock=row.stock_label+(f' · {row.entry_time} 입장 기준' if row.entry_time else '')
            self.table.insert('','end',values=('모노레일' if row.category=='monorail' else '입장권',row.time,stock,
                  ', '.join(AGE_NAMES[age] for age in row.eligible_ages) or '—'))
        self.status.set('감시 중 · 구매 가능 발견' if snapshot.hits else '감시 중 · 가용 회차 대기')
        self.session_status.set('NOL 연결 정상 · '+snapshot.checked_at)
        self.detail.set(date_label(snapshot.config.date)+' | '+count_label(snapshot.config.counts)+f"\n조회 {event['checks']}회 · 마지막 {snapshot.checked_at} · 목표 {', '.join(snapshot.config.time_specs)}")
        self.extra_label.set(snapshot.monorail_error or ('목표에 일치하는 회차가 없습니다. 날짜와 시각을 확인하세요.' if not snapshot.rounds else '공통 재고는 권종별 합계가 아닙니다. 구매 시 실시간 상태를 확인하세요.'))
        summary=' / '.join(row.time+' '+row.stock_label for row in snapshot.rounds)
        self._log('조회 완료 · '+(summary or '목표 회차 없음'))

    def _poll(self):
        if self.closed: return
        self.root.after_cancel(self.poll_id)
        while True:
            try: event=self.events.get_nowait()
            except queue.Empty: break
            self._event_persisted=event.get('_persisted',False)
            try: self._handle_event(event)
            finally: self._event_persisted=False
        self.poll_id=self.root.after(100,self._poll)

    def _handle_event(self,event):
        kind=event['kind']
        if kind=='session_maintenance':
            self._show_session_maintenance(event)
        elif kind in ('loaded','tested','login_opened','session_saved','catalog_done','job_error','test_pay_done'):
            self.busy=False
            booking=kind=='test_pay_done' or event.get('operation')=='test_pay_done'
            if booking:
                self._lock_conditions(False)
                message=('테스트 완료 · 최종 결제 미실행' if kind=='test_pay_done' else
                         '테스트 실패 · '+event['message'])
                self.operations.test_pay_status.set(message)
            if event.get('auth'): self.session_status.set('로그인 만료 · 전용 Edge에서 다시 로그인하세요')
            if kind in ('catalog_done','job_error'): self.syncing=False
            ticket=event.get('ticket')
            if ticket is not None and not self.guard.accepts(ticket):
                self._log('조건이 변경되어 이전 조회 결과를 제외했습니다. 다시 읽어주세요.',persist=True,event='STALE_RESULT')
                self.status.set('조건 변경 · 회차 새로고침 필요')
            elif kind=='loaded': self._show_loaded(event['value'])
            elif kind=='tested': self._log('텔레그램 테스트 알림 전송 완료')
            elif kind=='test_pay_done': self._log('브라우저 예약 테스트 완료 · 최종 결제 미실행')
            elif kind=='login_opened':
                if event['value']:
                    self.status.set('로그인 연결 완료 · 목록 읽기 또는 감시 시작이 가능합니다')
                    self.session_status.set('로그인 확인 · 세션 자동 저장 완료')
                    self._log('NOL 로그인 확인 · 세션 자동 저장 완료')
                else:
                    self.status.set('전용 Edge 로그인 후 로그인 세션 저장을 누르세요')
                    self.session_status.set('로그인 필요 · 전용 Edge에서 로그인 후 세션 저장하세요')
                    self._log('자동 로그인 미완료 · 위 안내에 따라 전용 Edge에서 로그인 후 세션을 저장하세요')
            elif kind=='session_saved':
                self.status.set('로그인 세션 저장 완료 · 브라우저 연결·목록 읽기를 누르세요')
                self.session_status.set('세션 저장 완료 · '+dt.datetime.now().strftime('%H:%M:%S'))
                self._log(f"로그인 확인 · 세션 쿠키 {event['value']}개 저장 · session_cookies.json")
            elif kind=='catalog_done':
                result=event['value'];self._cached_view()
                message=f"목록 {'확보 중지' if result['cancelled'] else '확보 완료'} · {result['days']}일 저장"
                if result['errors']: message+=f" · 확인 필요 {len(result['errors'])}건"
                self.status.set(message);self._log(message)
                for error in result['errors'][:5]: self._log(error)
            else: self.status.set(event['message']); self._log(event['message'])
            self._buttons()
        elif kind=='catalog_progress':
            self._update_product_stats();self.status.set(event['message']);self._log(event['message'])
        elif kind=='snapshot': self._show_snapshot(event)
        elif kind=='booking_attempt':
            self._log(event.get('message','가용 회차 발견 · 예약 시도'))
            # 가용 발견 시 실제 결제 시도 (백그라운드)
            if not self.busy and not getattr(self, 'test_booking_hold', False):
                self._start_auto_booking(event.get('snapshot'))
        elif kind=='started': self._log('감시 시작 · '+date_label(event['config'].date))
        elif kind=='finished':
            if not self.status.get().startswith('NOL') and '로그인' not in self.status.get(): self.status.set('감시 종료 · 조건을 변경할 수 있습니다')
            self._lock_conditions(False); self._buttons(); self._log(event['message'])
        elif kind in ('auth','error'):
            if kind=='auth': self.session_status.set('로그인 만료 · 전용 Edge에서 다시 로그인하세요')
            self.status.set(event['message']); self._log(event['message'])
        else: self._log(event.get('message',''))

    def _log(self,text,*,persist=None,level=logging.INFO,event='GUI_MESSAGE'):
        from storage import sanitize
        text=sanitize(text)
        if persist is None: persist=not self._event_persisted
        if persist: self.logger.log(level,text,extra={'event':event})
        stamp=dt.datetime.now().strftime('%H:%M:%S')
        for view in (self.home_log_text,self.log_text):
            at_bottom=not view.winfo_ismapped() or view.yview()[1]>=0.99
            view.configure(state='normal');view.insert('end',f'{stamp}  {text}\n')
            if int(view.index('end-1c').split('.')[0])>500: view.delete('1.0','100.0')
            if at_bottom: view.see('end')
            view.configure(state='disabled')

    def _problem(self,error):
        from storage import sanitize
        self._log(error,level=logging.ERROR,event='GUI_ERROR'); messagebox.showerror('화담숲 모니터',sanitize(error),parent=self.root)

    def _open_product(self):
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        webbrowser.open(url)

    def close(self):
        from storage import atomic_write, sanitize
        if self.closed: return
        self.logger.info('프로그램 종료 요청 · 감시/목록 작업 중지',extra={'event':'APP_CLOSING'})
        self.closed=True; self.runner.stop();self.catalog_stop.set(); self.root.after_cancel(self.poll_id)
        self.root.after_cancel(self.keepalive_id)
        if self.browser_open_id is not None: self.root.after_cancel(self.browser_open_id)
        self._remember()
        try: atomic_write(self.choices_file,json.dumps(self.choices,ensure_ascii=False,indent=2))
        except OSError as error:
            self.logger.error('종료 시 선택 저장 실패 · '+sanitize(error),extra={'event':'SETTINGS_SAVE_FAILED'})
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.root.destroy()
        with self.jobs_lock: pending=any(not future.done() for future in self.pending_jobs)
        if pending or self.runner.running:
            self.cleanup_thread=threading.Thread(target=self._finish_close,name='hwadam-cleanup',daemon=False)
            self.cleanup_thread.start()
        else: self._finish_close()

    def _finish_close(self):
        # 실행 중인 조회의 마지막 결과까지 보존하고, Tk 객체는 건드리지 않는다.
        self.executor.shutdown(wait=True,cancel_futures=True)
        if self.runner.thread: self.runner.thread.join(timeout=35)
        if self.runner.running:
            self.logger.warning('감시 종료 대기 35초 경과 · 마지막 조회가 끝날 때까지 기록 유지',extra={'event':'SHUTDOWN_WAITING'})
            self.runner.thread.join()
        try: self.browser.detach()
        except Exception as error:
            self.logger.warning('브라우저 분리 실패 · '+type(error).__name__,extra={'event':'BROWSER_DETACH_FAILED'})
        self.logger.info('프로그램 종료 · 작업 정리 완료',extra={'event':'APP_CLOSED'})
        self.log_handler.close()
