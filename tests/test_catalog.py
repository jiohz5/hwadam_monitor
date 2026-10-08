import json
from pathlib import Path
import tempfile
import threading
import unittest

from catalog import Catalogue, PRODUCTS, sync_catalogues
from model import parse_product
from test_api import FULL_PRODUCT


class CatalogueTests(unittest.TestCase):
    def test_resume_reads_only_incomplete_days_and_keeps_full_date_menu(self):
        class Client:
            def __init__(self): self.requests=[]
            def load_catalog(self,url,refresh=False): return parse_product(FULL_PRODUCT)
            def schedules(self,config): return [{'date':'2026-10-25'},{'date':'2026-10-26'}]
            def rounds(self,config,monorail=False,entry_time=None):
                self.requests.append(config.date)
                return []
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder));cache.put_rounds('10367229','2026-10-25',[])
            client=Client()
            result=sync_catalogues(client,cache,threading.Event(),lambda e:None,
                products=(PRODUCTS[0],),delay=0,today='2026-10-03',resume=True)
            self.assertEqual(client.requests,['2026-10-26'])
            self.assertEqual(result['days'],1)
            self.assertEqual(len(cache.product('10367229')['dates']),2)

    def test_saved_rounds_are_isolated_by_product_date_and_section(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder))
            cache.put_rounds('10367229','2026-10-25',[{'time':'08:00','roundStatusCode':'SOLD_OUT'}])
            cache.put_rounds('10367229','2026-10-25',[{'time':'08:06'}],section='MONORAIL_SEGMENT_1',parent_time='08:00')
            loaded=Catalogue(Path(folder))
            self.assertEqual(loaded.rounds('10367229','2026-10-25')['rows'][0]['time'],'08:00')
            self.assertIsNone(loaded.rounds('10323843','2026-10-25'))
            self.assertIsNone(loaded.rounds('10367229','2026-10-26'))
            self.assertIsNone(loaded.rounds('10367229','2026-10-25',section='MONORAIL_SEGMENT_2'))
            self.assertEqual(loaded.rounds('10367229','2026-10-25',section='MONORAIL_SEGMENT_1')['parent_time'],'08:00')

    def test_corrupt_cache_keeps_product_names_but_does_not_invent_times(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'catalog_cache.json').write_text('{broken',encoding='utf-8')
            cache=Catalogue(Path(folder))
            self.assertEqual(cache.product('10367229')['name'],'2026 화담숲 입장권 (10.23-11.15)')
            self.assertEqual(cache.product('10323843')['name'],'2026 화담숲 입장권(05.12-10.22)')
            self.assertIsNone(cache.rounds('10367229','2026-10-25'))
            self.assertTrue(cache.warning)

    def test_sync_keeps_sold_out_times_and_uses_actual_schedule_dates(self):
        class Client:
            def load_catalog(self,url,refresh=False): return parse_product(FULL_PRODUCT)
            def schedules(self,config): return [{'date':'2026-10-25'},{'date':'2026-10-26'}]
            def rounds(self,config,monorail=False,entry_time=None):
                return [{'time':'08:06' if monorail else '08:00','roundStatusCode':'SOLD_OUT','inventoryQuantity':0}]
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder))
            result=sync_catalogues(Client(),cache,threading.Event(),lambda e:None,
                                   products=(PRODUCTS[0],),delay=0,today='2026-10-03')
            self.assertEqual(result['days'],2)
            self.assertEqual(cache.product('10367229')['dates'],['2026-10-25','2026-10-26'])
            self.assertEqual(cache.rounds('10367229','2026-10-25')['rows'][0]['roundStatusCode'],'SOLD_OUT')
            self.assertIsNone(cache.rounds('10367229','2026-10-24'))

    def test_failed_read_does_not_replace_previously_saved_menu_with_empty_list(self):
        from api import ApiError
        class Client:
            def load_catalog(self,url,refresh=False): return parse_product(FULL_PRODUCT)
            def schedules(self,config): return [{'date':'2026-10-25'}]
            def rounds(self,*args,**kwargs): raise ApiError('접속 오류')
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder));cache.put_rounds('10367229','2026-10-25',[{'time':'08:20'}])
            result=sync_catalogues(Client(),cache,threading.Event(),lambda e:None,
                                   products=(PRODUCTS[0],),delay=0,today='2026-10-03')
            self.assertTrue(result['errors'])
            self.assertEqual(cache.rounds('10367229','2026-10-25')['rows'][0]['time'],'08:20')


if __name__=='__main__': unittest.main()
