import io
import json
import unittest
from urllib.error import HTTPError

from telegram_bot import TelegramError, TelegramNotifier


class TelegramTests(unittest.TestCase):
    def test_real_send_encodes_chat_and_text_and_checks_success(self):
        requests=[]
        def opener(request,timeout):
            requests.append(request)
            return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        notifier=TelegramNotifier('123456:abcdefghijk','-100123',opener=opener)
        notifier.send('경로 1장')
        body=json.loads(requests[0].data)
        self.assertEqual(body['chat_id'],'-100123')
        self.assertEqual(body['text'],'경로 1장')
        self.assertFalse(body['link_preview_options']['is_disabled'] is False)

    def test_api_failure_and_http_error_do_not_expose_bot_token(self):
        token='123456:abcdefghijk'
        def rejected(request,timeout):
            return io.BytesIO(b'{"ok":false,"description":"chat not found"}')
        with self.assertRaisesRegex(TelegramError,'chat not found'):
            TelegramNotifier(token,'1',opener=rejected).send('test')
        def http_error(request,timeout):
            raise HTTPError(request.full_url,401,'Unauthorized',{},io.BytesIO(b'{}'))
        with self.assertRaises(TelegramError) as caught:
            TelegramNotifier(token,'1',opener=http_error).send('test')
        self.assertNotIn(token,str(caught.exception))

    def test_empty_credentials_are_rejected_before_send(self):
        with self.assertRaises(TelegramError): TelegramNotifier('','').send('test')


if __name__=='__main__': unittest.main()
