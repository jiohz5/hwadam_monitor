"""중지 가능한 단일 감시 루프와 성공 기준 중복 알림 억제."""
from dataclasses import dataclass
import threading
import time

from api import AuthLost, Cancelled
from model import AGE_NAMES, SECTION_NAMES, Snapshot, count_label, date_label
from storage import sanitize


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
        tracker=AlertTracker()
        failures=0
        checks=0
        try:
            self._event('started',config=config)
            while not stop.is_set():
                delay=config.interval
                try:
                    result=self.client.check(config,stop)
                    if stop.is_set(): break
                    checks+=1
                    self._event('snapshot',snapshot=result,checks=checks)
                    if tracker.pending(result):
                        if stop.is_set(): break
                        notifier.send(format_alert(result))
                        tracker.ack(result)
                        self._event('sent',message='가용 회차 텔레그램 알림 전송 완료')
                    failures=0
                except Cancelled:
                    break
                except AuthLost as problem:
                    self._event('auth',message=sanitize(problem))
                    if not stop.is_set():
                        try:
                            notifier.send('화담숲 감시가 중지되었습니다. NOL 로그인 후 다시 시작하세요.\n'+date_label(config.date))
                        except Exception as error:
                            self._event('error',message=sanitize(error))
                    break
                except Exception as problem:
                    failures+=1
                    delay=min(300,max(config.interval,10)*(2**min(failures-1,4)))
                    self._event('error',message=sanitize(problem)+f' · {delay:g}초 후 재시도')
                stop.wait(delay)
        finally:
            self._event('finished',message='감시 종료')
