import threading
import time
import unittest

from model import Availability, Snapshot, WatchConfig
from monitor import AlertTracker, MonitorRunner, format_alert
from model import evaluate_round, parse_product
from test_model import PRODUCT


def snapshot(quantity=1, ages=('SENIOR',), *, full=False):
    return Snapshot(WatchConfig(), (Availability('08:00','entry',quantity,ages,full),))


class TrackerTests(unittest.TestCase):
    def test_first_hit_unchanged_suppression_and_stock_increase(self):
        tracker=AlertTracker()
        first=snapshot()
        self.assertTrue(tracker.pending(first))
        tracker.ack(first)
        self.assertFalse(tracker.pending(first))
        self.assertTrue(tracker.pending(snapshot(2)))

    def test_failure_stays_pending_and_sold_out_reappearance_alerts(self):
        tracker=AlertTracker()
        self.assertTrue(tracker.pending(snapshot()))
        self.assertTrue(tracker.pending(snapshot()))
        tracker.ack(snapshot())
        tracker.pending(snapshot(0,()))
        self.assertTrue(tracker.pending(snapshot()))

    def test_new_age_and_decrease_then_increase(self):
        tracker=AlertTracker()
        tracker.pending(snapshot(3)); tracker.ack(snapshot(3))
        self.assertFalse(tracker.pending(snapshot(1)))
        self.assertTrue(tracker.pending(snapshot(2)))
        tracker.ack(snapshot(2))
        self.assertTrue(tracker.pending(snapshot(2,('ADULT','SENIOR'))))

    def test_unconfirmed_state_does_not_become_a_false_sold_out_reappearance(self):
        tracker=AlertTracker()
        tracker.pending(snapshot()); tracker.ack(snapshot())
        unknown=Snapshot(WatchConfig(),(Availability('08:00','entry',None,(),False,'회차 상태 확인 필요'),))
        self.assertFalse(tracker.pending(unknown))
        self.assertFalse(tracker.pending(snapshot()))

    def test_message_distinguishes_shared_stock_from_goal(self):
        message=format_alert(snapshot())
        for text in ('2026-10-25(일)','성인 2장','경로 2장','어린이 2장','공통 잔여 1장','가능 권종: 경로'):
            self.assertIn(text,message)
        self.assertIn('10367229',message)

    def test_age_inventory_increase_alerts_even_with_unchanged_shared_stock(self):
        config=WatchConfig(counts={'SENIOR':3})
        group=parse_product(PRODUCT).groups['HWADAMSUP']
        def result(number):
            row=evaluate_round({'time':'08:00','roundStatusCode':'IN_SALE','inventoryQuantity':10},
                [{'productOptionItemId':14253582,'productOptionItemStatusCode':'IN_SALE','inventoryQuantity':number}],
                group,config,'entry')
            return Snapshot(config,(row,))
        tracker=AlertTracker()
        tracker.pending(result(1));tracker.ack(result(1))
        self.assertTrue(tracker.pending(result(2)))


class RunnerTests(unittest.TestCase):
    def test_stop_during_check_prevents_late_notification_and_second_start(self):
        entered=threading.Event(); release=threading.Event(); events=[]; sent=[]
        class Client:
            def check(self,config,stop):
                entered.set(); release.wait(2)
                return snapshot()
        class Notifier:
            def send(self,text): sent.append(text)
        runner=MonitorRunner(Client(),events.append)
        self.assertTrue(runner.start(WatchConfig(),Notifier()))
        self.assertTrue(entered.wait(1))
        self.assertFalse(runner.start(WatchConfig(),Notifier()))
        runner.stop(); release.set(); runner.thread.join(2)
        self.assertFalse(runner.running)
        self.assertEqual(sent,[])
        self.assertEqual(events[-1]['kind'],'finished')

    def test_successful_send_and_notification_failure_are_reported(self):
        finished=threading.Event(); events=[]
        class Client:
            def check(self,config,stop): return snapshot()
        class Notifier:
            def send(self,text):
                finished.set()
                raise RuntimeError('전송 실패')
        runner=MonitorRunner(Client(),events.append)
        runner.start(WatchConfig(),Notifier())
        self.assertTrue(finished.wait(1))
        deadline=time.monotonic()+1
        while not any(e['kind']=='error' for e in events) and time.monotonic()<deadline:
            time.sleep(.01)
        runner.stop(); runner.thread.join(2)
        self.assertTrue(any(e['kind']=='error' and '전송' in e['message'] for e in events))


if __name__=='__main__': unittest.main()
