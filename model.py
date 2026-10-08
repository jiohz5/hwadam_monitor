"""감시 조건과 재고 판정. 브라우저나 GUI에 의존하지 않는다."""
from dataclasses import dataclass, field
import datetime as dt
import math
import re
from urllib.parse import parse_qs, urlsplit


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
    interval: float = 10.0
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
        if not math.isfinite(float(self.interval)) or not 3 <= float(self.interval) <= 300:
            raise ValueError("조회 주기는 3~300초로 입력하세요")
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
