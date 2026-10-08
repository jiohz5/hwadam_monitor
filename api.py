"""NOL 상품 조회. 재고 선점이나 결제 요청을 수행하지 않는다."""
from dataclasses import dataclass, replace
import json
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from model import AGE_NAMES, OPEN_CODES, Catalog, Snapshot, WatchConfig, evaluate_round, parse_product, time_matches, validate_url


AUTH_CODES = {"leisure-web-api-0013", "leisure-web-api-0015", "leisure-web-api-1802", "leisure-web-api-1501"}


class ApiError(RuntimeError):
    pass


class AuthLost(ApiError):
    pass


class LoginRequired(AuthLost):
    pass


class Cancelled(Exception):
    pass


def decode_response(response: dict, expected):
    status = response.get("status")
    try:
        envelope = json.loads(response.get("body") or "{}")
        body = envelope.get("body")
    except (ValueError, AttributeError, TypeError):
        raise ApiError("조회 응답을 해석하지 못했습니다") from None
    code = body.get("code") if isinstance(body, dict) else None
    if status == 401 or code in AUTH_CODES:
        raise AuthLost("NOL 로그인 세션이 만료되었습니다")
    if code == "leisure-web-api-1110" and expected is list:
        return []
    if status != 200 or not isinstance(body, expected):
        raise ApiError(f"조회 실패: HTTP {status}" + (f" · {code}" if code else ""))
    if expected is list and any(not isinstance(item, dict) for item in body):
        raise ApiError("조회 목록 형식이 잘못되었습니다")
    return body


def group_for(catalog: Catalog, monorail=False):
    key = "MONORAIL" if monorail else "HWADAMSUP"
    group = catalog.groups.get(key)
    if monorail and group is None and catalog.additional_only and len(catalog.groups) == 1:
        group = next(iter(catalog.groups.values()))
    if group is None:
        raise ValueError("이 상품에서 " + ("모노레일" if monorail else "입장권") + " 옵션을 찾지 못했습니다")
    return group


def _params(config, group, monorail, entry_time=None):
    values = {"date":config.date,"productOptionGroupAttributeTypeCodes":"HWADAMSUP"}
    if monorail:
        section = group.sections.get(config.section)
        if not section:
            raise ValueError("선택한 모노레일 구간이 이 상품에 없습니다")
        values["productOptionItemIds"] = section
        values["productOptionAttributeDetailCodes"] = ",".join(config.selected_ages)
        if entry_time:
            values["time"] = entry_time
    return values


def rounds_path(catalog: Catalog, config: WatchConfig, *, monorail=False, entry_time=None):
    group = group_for(catalog,monorail)
    query = urlencode(_params(config,group,monorail,entry_time),safe=":,")
    return f"/product/v1/products/{catalog.id}/option-groups/{group.id}/rounds?{query}"


@dataclass(frozen=True)
class OrderInfo:
    date: str
    entry_time: str
    counts: dict[str,int]


def parse_order(body: dict, config: WatchConfig) -> OrderInfo:
    if body.get("date") != config.date:
        raise ValueError(f"선택 날짜 {config.date}와 주문 날짜 {body.get('date','미확인')}가 다릅니다")
    orders = body.get("orders")
    if not isinstance(orders,list) or not orders:
        raise ValueError("추가구매 가능한 입장권 주문을 확인하지 못했습니다")
    counts, times = {}, set()
    for order in orders:
        age = next((code for code in order.get("productOptionAttributeDetailCodes") or [] if code in AGE_NAMES),None)
        if not age:
            continue
        quantity = next((order[key] for key in ("remainingQuantity","additionalPurchaseAvailableQuantity","quantity")
                         if order.get(key) is not None),0)
        try:
            quantity = int(quantity)
        except (ValueError,TypeError,OverflowError):
            raise ValueError("주문의 추가구매 수량을 확인하지 못했습니다") from None
        counts[age] = counts.get(age,0) + max(0,quantity)
        if age in config.selected_ages:
            times.add(order.get("time"))
    for age in config.selected_ages:
        if config.counts[age] > counts.get(age,0):
            raise ValueError(f"{AGE_NAMES[age]} 목표 {config.counts[age]}장 · 이 주문의 추가구매 한도 {counts.get(age,0)}장")
    if len(times) != 1 or not next(iter(times),None):
        raise ValueError("여러 입장 회차가 포함되었거나 주문 입장 시각을 확인할 수 없습니다")
    entry_time = next(iter(times))
    if not time_matches(entry_time,("00:00~23:59",)):
        raise ValueError("주문 입장 시각 형식이 잘못되었습니다")
    return OrderInfo(config.date,entry_time,counts)


class LeisureClient:
    def __init__(self,transport):
        self.transport = transport
        self.lock = threading.RLock()
        self._catalog = None
        self._url = None
        self._order = None

    def load_catalog(self,url,*,refresh=False):
        validate_url(url)
        with self.lock:
            if refresh:
                self._catalog = self._url = self._order = None
                self.transport.connect(url,force=True)
            else:
                self.transport.connect(url)
            if self._url != url or self._catalog is None:
                self._catalog = parse_product(self.transport.product())
                self._url = url
                self._order = None
            return self._catalog

    def prepare(self,config: WatchConfig, *, refresh=False):
        config.validate()
        with self.lock:
            self.load_catalog(config.url,refresh=refresh)
            group = group_for(self._catalog,config.mode == "addon")
            if group.start and config.date < group.start or group.end and config.date > group.end:
                raise ValueError(f"이 상품의 예약 기간은 {group.start}~{group.end}입니다")
            if group.limit is not None and config.total > int(group.limit):
                raise ValueError(f"이 상품은 1인 {group.limit}장까지입니다. 권종 목표 합계를 줄이세요")
            missing = set(config.selected_ages) - set(group.ages.values())
            if missing:
                raise ValueError("이 상품에서 선택 권종을 찾지 못했습니다: " + ",".join(AGE_NAMES[key] for key in missing))
            if config.mode == "addon":
                self._order = self.verify_order(config)
            return self._catalog

    def catalog(self):
        if self._catalog is None:
            raise ApiError("먼저 브라우저를 연결하고 상품을 읽으세요")
        return self._catalog

    def _get(self,path,expected=list):
        try:
            return decode_response(self.transport.request(path),expected)
        except AuthLost:
            try:
                if self._url is None: raise
                self.transport.connect(self._url,force=True)
                return decode_response(self.transport.request(path),expected)
            except AuthLost:
                self._catalog = self._url = self._order = None
                raise

    def schedules(self,config):
        group = group_for(self.catalog(),config.mode == "addon")
        if config.mode == "addon":
            return [{"date":config.date,"scheduleStatusCode":"IN_SALE"}]
        params = urlencode({"startDate":group.start or config.date,"endDate":group.end or config.date})
        return self._get(f"/product/v1/products/{self.catalog().id}/option-groups/{group.id}/schedules?{params}")

    def rounds(self,config, *, monorail=False,entry_time=None):
        rows = self._get(rounds_path(self.catalog(),config,monorail=monorail,entry_time=entry_time))
        if any(not time_matches(row.get("time",""),("00:00~23:59",)) or
               row.get('roundStatusCode') is not None and not isinstance(row['roundStatusCode'],str) for row in rows):
            raise ApiError("회차의 시각을 해석하지 못했습니다")
        return sorted(rows,key=lambda row:row["time"])

    def age_items(self,config,time, *, monorail=False):
        catalog = self.catalog()
        group = group_for(catalog,monorail)
        if not group.age_option_id:
            raise ApiError("권종 조회 옵션을 찾지 못했습니다")
        params = _params(config,group,monorail)
        params["time"] = time
        path = f"/product/v1/products/{catalog.id}/option-groups/{group.id}/options/{group.age_option_id}/round-option-items?"
        return self._get(path + urlencode(params,safe=":,"))

    def verify_order(self,config):
        order_id = parse_qs(urlsplit(config.addon_url).query)["orderGroupId"][0]
        path = f"/order/v1/orders/group/{order_id}/additional-purchase/verification"
        response = self.transport.request(path,method="POST",data={"productId":self.catalog().id})
        return parse_order(decode_response(response,dict),config)

    @staticmethod
    def _cancel(stop):
        if stop.is_set():
            raise Cancelled()

    def _evaluate(self,config,raw,stop, *, monorail=False,allowed_ages=None,query_config=None):
        self._cancel(stop)
        group = group_for(self.catalog(),monorail)
        items = []
        if raw.get("roundStatusCode") in OPEN_CODES and raw.get("inventoryQuantity") != 0:
            items = self.age_items(query_config or config,raw["time"],monorail=monorail)
        self._cancel(stop)
        try:
            return evaluate_round(raw,items,group,config,"monorail" if monorail else "entry",allowed_ages=allowed_ages)
        except (TypeError,ValueError,KeyError):
            raise ApiError('권종 재고 응답을 해석하지 못했습니다') from None

    def check(self,config: WatchConfig,stop: threading.Event) -> Snapshot:
        with self.lock:
            self._cancel(stop)
            self.prepare(config)
            self._cancel(stop)
            mono = config.mode == "addon"
            parent_time = self._order.entry_time if mono else None
            rows = self.rounds(config,monorail=mono,entry_time=parent_time)
            selected = [row for row in rows if time_matches(row["time"],config.time_specs)
                        and (not parent_time or row["time"] >= parent_time)]
            allowed = self._order.counts.keys() if mono else None
            results = tuple(self._evaluate(config,row,stop,monorail=mono,allowed_ages=allowed) for row in selected)
            extra, error = (), ""
            hits = [result for result in results if result.available]
            if not mono and config.include_monorail and hits:
                try:
                    pairs=[]
                    for entry in hits:
                        self._cancel(stop)
                        scoped=replace(config,counts={age:config.counts.get(age,0) if age in entry.eligible_ages else 0 for age in AGE_NAMES})
                        rows=self.rounds(scoped,monorail=True,entry_time=entry.time)
                        specs=config.monorail_specs_for(entry.time)
                        candidates=[row for row in rows if row['time']>=entry.time
                                    and (time_matches(row['time'],specs) if specs or config.mono_relative else True)
                                    and row.get('roundStatusCode') in OPEN_CODES and row.get('inventoryQuantity')!=0]
                        if not specs and not config.mono_relative: candidates=candidates[:8]
                        pairs.extend(replace(self._evaluate(config,row,stop,monorail=True,
                                             allowed_ages=entry.eligible_ages,query_config=scoped),entry_time=entry.time) for row in candidates)
                    extra=tuple(pairs)
                except Cancelled:
                    raise
                except (ApiError,ValueError) as problem:
                    error = str(problem)
            self._cancel(stop)
            return Snapshot(config,results,extra,error)
