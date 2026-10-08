"""상품별 탐색 목록 저장. 저장된 목록은 실시간 재고 판정에 사용하지 않는다."""
from copy import deepcopy
from dataclasses import replace
import datetime as dt
import json
from pathlib import Path
import threading

from api import ApiError, AuthLost, Cancelled, group_for
from model import AGE_NAMES, DEFAULT_URL, REFERENCE_URL, KST, WatchConfig, time_matches
from storage import atomic_write, sanitize


PRODUCTS=(
    {'id':'10367229','url':DEFAULT_URL,'name':'2026 화담숲 입장권 (10.23-11.15)',
     'start':'2026-10-23','end':'2026-11-15','default_date':'2026-10-25'},
    {'id':'10323843','url':REFERENCE_URL,'name':'2026 화담숲 입장권(05.12-10.22)',
     'start':'2026-05-12','end':'2026-10-22','default_date':'2026-10-03'},
)


def product_id(url):
    return str(url).split('?')[0].rstrip('/').rsplit('/',1)[-1]


def now(): return dt.datetime.now(KST).isoformat(timespec='seconds')


class Catalogue:
    def __init__(self,base):
        self.path=Path(base)/'catalog_cache.json'
        self.lock=threading.RLock()
        self.warning=''
        self.products={p['id']:{**p,'dates':[],'days':{}} for p in PRODUCTS}
        for path in (Path(base)/'data'/'catalog_seed.json',self.path):
            if not path.exists(): continue
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if data.get('schema')!=1 or not isinstance(data.get('products'),dict): raise ValueError()
                for pid,item in data['products'].items():
                    if pid not in self.products or not isinstance(item,dict): continue
                    if not isinstance(item.get('days',{}),dict) or not isinstance(item.get('dates',[]),list): raise ValueError()
                    self.products[pid].update(item)
                    self.products[pid]['id']=pid
            except (OSError,ValueError,TypeError,AttributeError):
                self.warning='저장된 상품 목록 일부를 읽지 못했습니다. 목록 확보로 갱신하세요.'

    def _save(self):
        atomic_write(self.path,json.dumps({'schema':1,'products':self.products},ensure_ascii=False,indent=2))

    def product(self,pid):
        with self.lock: return deepcopy(self.products.get(str(pid)))

    def put_catalog(self,catalog,dates):
        pid=str(catalog.id)
        if pid not in self.products: return
        group=group_for(catalog)
        validated=[]
        for row in dates:
            value=row.get('date')
            try: dt.date.fromisoformat(value)
            except (ValueError,TypeError): raise ApiError('날짜 목록을 해석하지 못했습니다') from None
            validated.append(value)
        with self.lock:
            self.products[pid].update(name=catalog.name,start=group.start,end=group.end,
                                     dates=sorted(set(validated)),catalog_at=now())
            self._save()

    def put_rounds(self,pid,date,rows,*,section='',parent_time='',ages=()):
        if str(pid) not in self.products: return
        dt.date.fromisoformat(date)
        clean=[]
        for row in rows:
            if not isinstance(row,dict) or not time_matches(row.get('time'),('00:00~23:59',)):
                raise ApiError('저장할 회차의 시각을 해석하지 못했습니다')
            clean.append({key:row[key] for key in ('time','roundStatusCode','inventoryQuantity') if key in row})
        record={'rows':sorted(clean,key=lambda row:row['time']),'observed_at':now(),
                'parent_time':parent_time,'ages':list(ages)}
        with self.lock:
            day=self.products[str(pid)].setdefault('days',{}).setdefault(date,{})
            day[section or 'entry']=record
            self._save()

    def rounds(self,pid,date,*,section=''):
        with self.lock:
            value=self.products.get(str(pid),{}).get('days',{}).get(date,{}).get(section or 'entry')
            return deepcopy(value)

    def summary(self,pid):
        product=self.product(pid) or {}
        days=product.get('days',{})
        stored=sum('entry' in day for day in days.values())
        return stored,len(product.get('dates',[]))


def sync_catalogues(client,cache,stop,emit,*,products=PRODUCTS,delay=.25,today=None,resume=False):
    """실제 예약 날짜별 입장권과 구간별 모노레일 메뉴를 확보한다."""
    today=today or dt.datetime.now(KST).date().isoformat()
    total_days=0; errors=[]
    def checkpoint():
        if stop.wait(delay): raise Cancelled()
    try:
        for product in products:
            checkpoint()
            catalog=client.load_catalog(product['url'],refresh=True)
            group=group_for(catalog)
            counts={age:1 for age in AGE_NAMES if age in group.ages.values()}
            config=WatchConfig(product_url=product['url'],date=max(today,group.start or today),counts=counts)
            dates=client.schedules(config)
            cache.put_catalog(catalog,dates)
            days=[row['date'] for row in dates if row['date']>=today]
            sections=catalog.groups.get('MONORAIL')
            consecutive=0
            for index,date in enumerate(days):
                checkpoint()
                if resume:
                    saved=cache.rounds(catalog.id,date)
                    if saved is not None and (not saved['rows'] or not sections or all(
                        cache.rounds(catalog.id,date,section=section) is not None for section in sections.sections)):
                        continue
                selected=replace(config,date=date)
                try:
                    entry=client.rounds(selected)
                    cache.put_rounds(catalog.id,date,entry)
                    consecutive=0
                    total_days+=1
                except AuthLost: raise
                except ApiError as error:
                    errors.append(f'{date} 입장권: '+sanitize(error));consecutive+=1
                    if consecutive>=3: raise ApiError('연속 조회 실패 · 확보한 목록은 저장되어 있습니다') from error
                    continue
                if entry and sections:
                    parent=min(row['time'] for row in entry)
                    for section in sections.sections:
                        checkpoint()
                        try:
                            scoped=replace(selected,section=section)
                            rows=client.rounds(scoped,monorail=True,entry_time=parent)
                            cache.put_rounds(catalog.id,date,rows,section=section,parent_time=parent,ages=scoped.selected_ages)
                        except AuthLost: raise
                        except ApiError as error: errors.append(f'{date} {section}: '+sanitize(error))
                emit({'kind':'catalog_progress','message':f"{catalog.name} · {date} · {index+1}/{len(days)}일 저장",'pid':str(catalog.id)})
        return {'days':total_days,'errors':errors,'cancelled':False}
    except Cancelled:
        return {'days':total_days,'errors':errors,'cancelled':True}
