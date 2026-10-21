"""GUI 객체나 비밀 설정을 읽지 않고 작업 이벤트를 진단 기록으로 변환한다."""
import logging

from catalog import product_id
from gui import AGE_NAMES, BookingResult, booking_result_message, count_label, date_label


def watch_summary(config):
    mono = ('각 입장 +20분 ±15분' if config.mono_relative else ', '.join(config.mono_specs) or '전체 회차')
    return (f'product={product_id(config.product_url)} mode={config.mode} · {date_label(config.date)}'
            f" · 시간={', '.join(config.time_specs)} · {count_label(config.counts)}"
            f' · 모노레일={mono if config.include_monorail or config.mode=="addon" else "제외"}'
            f' section={config.section} interval={config.interval:g}s repeat={config.repeat_seconds:g}s')


def rows_summary(rows):
    values = []
    for row in rows:
        text = f'{row.time} {row.stock_label}'
        if row.entry_time: text += f' (입장 기준={row.entry_time})'
        if row.eligible_ages: text += ' 가능권종=' + ','.join(AGE_NAMES[age] for age in row.eligible_ages)
        if row.available: text += ' 목표전량=' + ('가능' if row.full_target else '불가')
        values.append(text)
    return ' / '.join(values) or '목표 회차 없음'


def event_records(event):
    """(level, code, message) 목록. URL·주문·쿠키·이벤트 전체 repr은 저장하지 않는다."""
    kind = event['kind']; records = []
    def add(code, message, level=logging.INFO): records.append((level, code, message))
    if kind == 'started':
        add('MONITOR_STARTED', '감시 시작 · ' + watch_summary(event['config']))
    elif kind == 'snapshot':
        value = event['snapshot']
        label = '모노레일' if value.config.mode == 'addon' else '입장권'
        add('MONITOR_CHECK', f"조회 완료 · checks={event['checks']} date={value.config.date} · {label} · " + rows_summary(value.rounds))
        if value.config.include_monorail and value.config.mode == 'entry':
            add('MONORAIL_CHECK', '모노레일 조회 완료 · ' + rows_summary(value.monorail))
        if value.monorail_error:
            add('MONORAIL_CHECK_FAILED', '모노레일 조회 확인 필요 · ' + value.monorail_error, logging.WARNING)
    elif kind == 'sent':
        message = event['message']
        if event.get('snapshot'):
            value = event['snapshot']
            message += f' · date={value.config.date} · ' + rows_summary((*value.hits, *(row for row in value.monorail if row.available)))
        add('TELEGRAM_SENT', message)
    elif kind == 'finished':
        add('MONITOR_FINISHED', f"{event['message']} · reason={event.get('reason','unknown')} checks={event.get('checks',0)}")
    elif kind in ('auth', 'error', 'job_error', 'catalog_error'):
        code = {'auth':'AUTH_LOST', 'error':'MONITOR_ERROR', 'job_error':'JOB_FAILED', 'catalog_error':'CATALOG_FAILED'}[kind]
        context = ' '.join(f'{key}={event[key]}' for key in ('operation','phase','failures','retry_seconds','date','product_id','section') if key in event)
        add(code, context + ' · ' + event['message'], logging.ERROR)
    elif kind == 'loaded':
        config, catalog, dates, primary, secondary, _, extra_error = event['value']
        add('CATALOG_LOADED', f'목록 읽기 완료 · product={catalog.id} · {catalog.name} · {config.date} · 날짜 {len(dates)}개 · 입장/주 회차 {len(primary)}개 · 모노레일 {len(secondary)}개')
        if extra_error: add('MONORAIL_LOAD_FAILED', '모노레일 목록 확인 필요 · ' + extra_error, logging.WARNING)
    elif kind == 'session_maintenance':
        if 'message' in event:
            add('SESSION_MAINTENANCE_FAILED', '로그인 유지 실패 · ' + event['message'], logging.WARNING)
        else:
            value = event['value']
            if value['state'] in ('checked','renewed'):
                add('SESSION_MAINTENANCE', f"로그인 유지 · state={value['state']} expires_at={value.get('expires_at',0)}")
    elif kind == 'catalog_done':
        value = event['value']
        add('CATALOG_DONE', f"목록 {'확보 중지' if value['cancelled'] else '확보 완료'} · {value['days']}일 저장 · 확인 필요 {len(value['errors'])}건")
    elif kind == 'test_pay_done':
        result=event.get('value')
        status=result.status if isinstance(result,BookingResult) else ''
        code={'confirmed':'BOOKING_CONFIRMED','unconfirmed':'BOOKING_RESULT_UNCONFIRMED'}.get(status,'BOOKING_TEST_DONE')
        add(code,booking_result_message(result),logging.WARNING if status=='unconfirmed' else logging.INFO)
    elif kind == 'test_pay_cancelled': add('BOOKING_TEST_CANCELLED', '브라우저 테스트 중지 완료')
    elif kind == 'tested': add('TELEGRAM_TEST_SENT', '텔레그램 테스트 알림 전송 완료')
    elif kind == 'session_saved': add('SESSION_SAVED', f"로그인 확인 · 세션 쿠키 {event['value']}개 저장")
    elif kind == 'login_opened':
        add('LOGIN_OPENED', 'NOL 로그인 확인 · 세션 자동 저장 완료' if event['value'] else '자동 로그인 미완료 · 전용 Edge에서 로그인 후 세션 저장 필요')
    elif event.get('message'):
        add('CATALOG_PROGRESS' if kind == 'catalog_progress' else 'BROWSER' if kind == 'log' else 'APP', event['message'])
    return records


def persist_event(logger, event):
    for level, code, message in event_records(event):
        logger.log(level, message, extra={'event':code})
