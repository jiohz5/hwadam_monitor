"""텔레그램 전송 전용. 기존 봇의 명령 수신과 충돌하지 않는다."""
import json
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(self,token,chat_id,*,opener=None):
        self.token=token.strip()
        self.chat_id=chat_id.strip()
        self.opener=opener or urllib.request.urlopen

    def validate(self):
        if not self.token or not self.chat_id:
            raise TelegramError('텔레그램 봇 토큰과 채팅 ID를 입력하세요')
        if any(char.isspace() for char in self.token) or '/' in self.token:
            raise TelegramError('텔레그램 봇 토큰 형식을 확인하세요')
        return self

    def send(self,text):
        self.validate()
        if len(text)>4000:
            text=text[:3900]+'\n(나머지 회차는 GUI에서 확인하세요)'
        body={'chat_id':self.chat_id,'text':text,'link_preview_options':{'is_disabled':True}}
        request=urllib.request.Request('https://api.telegram.org/bot'+self.token+'/sendMessage',
            data=json.dumps(body,ensure_ascii=False).encode('utf-8'),headers={'Content-Type':'application/json'},method='POST')
        try:
            with self.opener(request,timeout=15) as response:
                result=json.load(response)
        except urllib.error.HTTPError as error:
            raise TelegramError(f'텔레그램 전송 실패 · HTTP {error.code}. 토큰과 채팅 ID를 확인하세요') from None
        except (OSError,ValueError,TypeError) as error:
            raise TelegramError('텔레그램 연결 또는 응답 오류 · 인터넷 연결을 확인하세요') from None
        if not isinstance(result,dict) or result.get('ok') is not True:
            description=result.get('description','응답 확인 필요') if isinstance(result,dict) else '응답 확인 필요'
            raise TelegramError('텔레그램 전송 실패 · '+str(description).replace(self.token,'<봇 토큰 숨김>'))
        return result.get('result')
