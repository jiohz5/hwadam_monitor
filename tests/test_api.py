import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from api import ApiError, AuthLost, LeisureClient, parse_order, rounds_path, decode_response
from model import WatchConfig, parse_product
from test_model import PRODUCT


MONO = {"productOptionGroupId":10480708,"productOptionGroupAttributeTypeCode":"MONORAIL",
        "reservationStartDate":"2026-10-23","reservationEndDate":"2026-11-15","quantityPerPerson":6,
        "productOptions":[
            {"productOptionId":10652046,"productOptionAttributeCode":"MONORAIL_SEGMENT",
             "productOptionItems":[{"productOptionItemId":14253600,"productOptionAttributeDetailCode":"MONORAIL_SEGMENT_2"}]},
            {"productOptionId":10652047,"productOptionAttributeCode":"AGE","productOptionItems":[
                {"productOptionItemId":14254402,"productOptionAttributeDetailCode":"ADULT"},
                {"productOptionItemId":14254403,"productOptionAttributeDetailCode":"SENIOR"},
                {"productOptionItemId":14254405,"productOptionAttributeDetailCode":"CHILD"}]}]}
FULL_PRODUCT = dict(PRODUCT,productOptionGroups=[*PRODUCT['productOptionGroups'],MONO])


class ApiContractTests(unittest.TestCase):
    def test_expired_api_auth_gets_one_session_reconnect_before_stopping(self):
        for recovers in (True,False):
            with self.subTest(recovers=recovers):
                class Transport:
                    def __init__(self): self.authorized=False;self.reconnects=0;self.requests=0
                    def connect(self,url,*,force=False):
                        if force: self.reconnects+=1;self.authorized=recovers
                    def product(self): return FULL_PRODUCT
                    def request(self,path):
                        self.requests+=1
                        if not self.authorized: return {'status':401,'body':'{}'}
                        return {'status':200,'body':json.dumps({'body':[{'time':'08:00','roundStatusCode':'SOLD_OUT','inventoryQuantity':0}]})}
                transport=Transport();client=LeisureClient(transport)
                if recovers:
                    result=client.check(WatchConfig(),threading.Event())
                    self.assertEqual(result.rounds[0].reason,'품절')
                else:
                    with self.assertRaises(AuthLost): client.check(WatchConfig(),threading.Event())
                    self.assertIsNone(client._catalog)
                self.assertEqual(transport.reconnects,1)
                self.assertEqual(transport.requests,2)

    def test_relative_monorail_window_is_evaluated_for_each_entry_independently(self):
        class Transport:
            def connect(self,url): pass
            def product(self): return FULL_PRODUCT
            def request(self,path,**kwargs):
                if path.split('?')[0].endswith('/rounds'):
                    times=('08:04','08:05','08:25','08:35','08:36','08:55','08:56') if '/10480708/' in path else ('08:00','08:20')
                    body=[{'time':time,'roundStatusCode':'IN_SALE','inventoryQuantity':1} for time in times]
                else:
                    body=[{'productOptionItemId':14254403 if '/10480708/' in path else 14253582,'productOptionItemStatusCode':'IN_SALE'}]
                return {'status':200,'body':json.dumps({'body':body})}
        config=WatchConfig(include_monorail=True,mono_relative=True)
        result=LeisureClient(Transport()).check(config,threading.Event())
        pairs={(row.entry_time,row.time) for row in result.monorail}
        self.assertEqual(pairs,{('08:00','08:05'),('08:00','08:25'),('08:00','08:35'),
                                ('08:20','08:25'),('08:20','08:35'),('08:20','08:36'),('08:20','08:55')})

    def test_round_query_uses_current_product_group_segment_and_parent_entry_time(self):
        catalog=parse_product(FULL_PRODUCT)
        path=rounds_path(catalog,WatchConfig(),monorail=True,entry_time='08:00')
        self.assertEqual(urlsplit(path).path,'/product/v1/products/10367229/option-groups/10480708/rounds')
        query=parse_qs(urlsplit(path).query)
        self.assertEqual(query['productOptionItemIds'],['14253600'])
        self.assertEqual(query['time'],['08:00'])
        self.assertEqual(query['productOptionAttributeDetailCodes'],['ADULT,SENIOR,CHILD'])
        self.assertEqual(query['productOptionGroupAttributeTypeCodes'],['HWADAMSUP'])

    def test_authentication_and_malformed_errors_are_not_empty_inventory(self):
        for response, exception in (
            ({'status':401,'body':'{}'},AuthLost),
            ({'status':200,'body':json.dumps({'body':{'code':'leisure-web-api-0013'}})},AuthLost),
            ({'status':200,'body':'bad'},ApiError),
            ({'status':429,'body':'{}'},ApiError),
            ({'status':200,'body':json.dumps({'body':{'unexpected':True}})},ApiError)):
            with self.subTest(response=response), self.assertRaises(exception):
                decode_response(response,list)

    def test_explicit_no_round_code_is_a_valid_empty_result(self):
        response={'status':400,'body':json.dumps({'body':{'code':'leisure-web-api-1110'}})}
        self.assertEqual(decode_response(response,list),[])

    def test_order_mismatch_and_ineligible_age_are_rejected(self):
        body={'date':'2026-10-03','orders':[{'time':'09:00','quantity':2,'productOptionAttributeDetailCodes':['ADULT']}]}
        with self.assertRaisesRegex(ValueError,'날짜'):
            parse_order(body,WatchConfig())
        config=WatchConfig(date='2026-10-03',counts={'ADULT':2,'SENIOR':2,'CHILD':0,'TEEN':0})
        with self.assertRaisesRegex(ValueError,'경로'):
            parse_order(body,config)

    def test_remaining_order_quantity_limits_each_age(self):
        body={'date':'2026-10-25','orders':[
            {'time':'08:00','quantity':2,'remainingQuantity':1,'productOptionAttributeDetailCodes':['ADULT']},
            {'time':'08:00','quantity':2,'productOptionAttributeDetailCodes':['SENIOR']},
            {'time':'08:00','quantity':2,'productOptionAttributeDetailCodes':['CHILD']}]}
        with self.assertRaisesRegex(ValueError,'성인'):
            parse_order(body,WatchConfig())

    def test_order_eligibility_preserves_adult_senior_child_codes(self):
        body={'date':'2026-10-25','orders':[
            {'time':'08:00','quantity':2,'productOptionAttributeDetailCodes':[age,'NONE']}
            for age in ('ADULT','SENIOR','CHILD')]}
        order=parse_order(body,WatchConfig())
        self.assertEqual(order.counts,{'ADULT':2,'SENIOR':2,'CHILD':2})
        self.assertEqual(order.entry_time,'08:00')

    def test_monorail_failure_does_not_drop_one_available_entry_ticket(self):
        class Transport:
            def connect(self,url):
                pass
            def product(self):
                return FULL_PRODUCT
            def request(self,path,**kwargs):
                if '/10480708/' in path:
                    raise ApiError('모노레일 조회 실패')
                if path.split('?')[0].endswith('/rounds'):
                    body=[{'time':'08:00','roundStatusCode':'IN_SALE','inventoryQuantity':1}]
                else:
                    body=[{'productOptionItemId':14253582,'productOptionItemStatusCode':'IN_SALE'}]
                return {'status':200,'body':json.dumps({'body':body})}
        snapshot=LeisureClient(Transport()).check(WatchConfig(include_monorail=True),threading.Event())
        self.assertEqual(len(snapshot.hits),1)
        self.assertEqual(snapshot.hits[0].eligible_ages,('SENIOR',))
        self.assertIn('실패',snapshot.monorail_error)

    def test_malformed_monorail_time_does_not_block_available_entry_alert(self):
        class Transport:
            def connect(self,url): pass
            def product(self): return FULL_PRODUCT
            def request(self,path,**kwargs):
                if '/10480708/' in path:
                    body=[{'time':None,'roundStatusCode':'IN_SALE','inventoryQuantity':1}]
                elif path.split('?')[0].endswith('/rounds'):
                    body=[{'time':'08:00','roundStatusCode':'IN_SALE','inventoryQuantity':1}]
                else:
                    body=[{'productOptionItemId':14253582,'productOptionItemStatusCode':'IN_SALE'}]
                return {'status':200,'body':json.dumps({'body':body})}
        result=LeisureClient(Transport()).check(WatchConfig(include_monorail=True),threading.Event())
        self.assertEqual(len(result.hits),1)
        self.assertTrue(result.monorail_error)

    def test_monorail_age_is_paired_to_its_own_entry_time(self):
        class Transport:
            def connect(self,url): pass
            def product(self): return FULL_PRODUCT
            def request(self,path,**kwargs):
                query=parse_qs(urlsplit(path).query)
                if '/10480708/' in path:
                    if path.split('?')[0].endswith('/rounds'):
                        body=[{'time':'08:10','roundStatusCode':'IN_SALE','inventoryQuantity':2}]
                    else:
                        body=[{'productOptionItemId':14254402,'productOptionItemStatusCode':'IN_SALE'},
                              {'productOptionItemId':14254403,'productOptionItemStatusCode':'IN_SALE'}]
                elif path.split('?')[0].endswith('/rounds'):
                    body=[{'time':t,'roundStatusCode':'IN_SALE','inventoryQuantity':1} for t in ('08:00','08:20')]
                else:
                    item=14253582 if query['time']==['08:00'] else 14253581
                    body=[{'productOptionItemId':item,'productOptionItemStatusCode':'IN_SALE'}]
                return {'status':200,'body':json.dumps({'body':body})}
        result=LeisureClient(Transport()).check(WatchConfig(include_monorail=True),threading.Event())
        self.assertTrue(result.monorail)
        self.assertEqual(result.monorail[0].eligible_ages,('SENIOR',))


if __name__=='__main__':
    unittest.main()
