"""Recognize access challenges without solving CAPTCHAs or storing tokens."""
import re
import time
from email.utils import parsedate_to_datetime


class SourceAccessError(RuntimeError):
    def __init__(self,kind,status_code=None,retry_after_s=None):
        super().__init__('Source access '+kind)
        self.kind=kind;self.status_code=status_code;self.retry_after_s=retry_after_s


def response_access_issue(response,*,has_data=False):
    if has_data:return None
    body=getattr(response,'text','').lower()
    status=response.status_code
    title=re.search(r'<title\b[^>]*>(.*?)</title>',body,re.S)
    heading=title[1] if title else ''
    if ('are you not a robot' in heading or 'robot' in heading and 'captcha' in body
            or 'подтвердите, что вы не робот' in heading or '/showcaptcha' in str(getattr(response,'url','')).lower()
            or (len(body)<50000 and 'smartcaptcha' in body and 'requests sent from your device are automated' in body)):
        kind='captcha'
    elif (len(body)<50000 and re.search(
            r'<script\b[^>]*src=[\"\']https?://(?:[a-z0-9-]+\.)?servicepipe\.tech/static/checkjs/', body)):
        kind='security_challenge'
    elif status==429:kind='rate_limited'
    elif status==439:kind='security_challenge'
    elif status==403:kind='forbidden'
    else:return None
    headers=getattr(response,'headers',{}) or {}
    value=headers.get('Retry-After',0)
    try:retry=float(value)
    except (ValueError,TypeError):
        try:retry=parsedate_to_datetime(value).timestamp()-time.time()
        except (ValueError,TypeError,OverflowError):retry=0
    if not 0<retry<=3600:retry=300 if kind=='captcha' else 60
    return dict(kind=kind,status_code=status,retry_after_s=retry,
                requires_human_verification=kind=='captcha')
