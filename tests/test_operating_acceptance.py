"""Operating point: concurrent requests share one account's hard 40 RPM cap."""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.ai_contract import AITransportError,AI_DEADLINE
from real_estate_engine.ai_pacing import AIRequestPacer, shared_request_pacer
from real_estate_engine.competitors import NvidiaCompetitorAnalyzer


class QueueAcceptanceTests(unittest.TestCase):
    def analyzer(self,pacer,post):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='primary';analyzer.endpoint='https://example.invalid/controlled'
        analyzer.floor_request_pacer=pacer;analyzer.http_session=SimpleNamespace(post=post)
        return analyzer

    @staticmethod
    def response(status=200):
        body={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        return SimpleNamespace(status_code=status,headers={},content=json.dumps(body).encode())

    def test_concurrent_clients_stop_at_40_and_resume_in_next_window(self):
        first=shared_request_pacer('operating-acceptance-fixture-account',min_interval=0,requests_per_minute=40)
        second=shared_request_pacer('operating-acceptance-fixture-account',min_interval=0,requests_per_minute=40)
        self.assertIs(first,second)
        # Isolate this test from repeated runs in the same interpreter.
        with first._state:
            first._http_starts.clear();first.http_attempts=0;first._not_before=0
        clock=[100.0];starts=[];lock=threading.Lock()
        def post(*args,**kwargs):
            with lock:starts.append(clock[0])
            return self.response()
        clients=[self.analyzer(first,post),self.analyzer(second,post)]
        def request(i):
            try:
                clients[i%2]._call_nvidia_sync('Controlled request',timeout=.01)
                return 'success'
            except AITransportError as exc:
                self.assertEqual(exc.status_code,429)
                return 'waiting'
        with patch('real_estate_engine.ai_pacing.time.monotonic',side_effect=lambda:clock[0]):
            with ThreadPoolExecutor(max_workers=12) as executor:results=list(executor.map(request,range(48)))
            self.assertEqual(results.count('success'),40);self.assertEqual(results.count('waiting'),8)
            self.assertEqual(len(starts),40)
            clock[0]=160.001
            self.assertEqual([request(i) for i in range(8)],['success']*8)
        self.assertEqual(first.http_attempts,48)
        self.assertEqual(max(sum(t<=s<t+60 for s in starts) for t in starts),40)

    def test_explicit_models_consume_the_same_http_budget(self):
        pacer=AIRequestPacer(0,40);models=[]
        def post(*args,**kwargs):
            model=kwargs['json']['model'];models.append(model)
            return self.response(503 if model=='primary' else 200)
        analyzer=self.analyzer(pacer,post)
        # This tests shared request accounting, not the host's ability to
        # schedule a mocked HTTP exchange within ten real milliseconds.
        with patch('real_estate_engine.ai_pacing.time.monotonic', return_value=100.0):
            for _ in range(20):
                with self.assertRaises(AITransportError) as unavailable:
                    analyzer._call_nvidia_sync('Controlled request',timeout=.01)
                self.assertEqual(unavailable.exception.status_code,503)
                analyzer._call_nvidia_sync('Independent verification',model_override='reviewer',timeout=.01)
            with self.assertRaises(AITransportError) as error:analyzer._call_nvidia_sync('No slot',timeout=.01)
        self.assertEqual(error.exception.status_code,429)
        self.assertEqual(models.count('primary'),20);self.assertEqual(models.count('reviewer'),20)
        self.assertEqual(pacer.http_attempts,40)

    def test_expired_search_deadline_prevents_a_late_thread_from_sending_http(self):
        import time
        posts=[];analyzer=self.analyzer(AIRequestPacer(0),lambda *a,**kw:posts.append(kw))
        token=AI_DEADLINE.set(time.monotonic()-1)
        try:
            with self.assertRaises(AITransportError):analyzer._call_nvidia_sync('Expired')
        finally:AI_DEADLINE.reset(token)
        self.assertFalse(posts);self.assertEqual(analyzer.floor_request_pacer.http_attempts,0)

    def test_waiting_for_queue_consumes_the_same_request_time_budget(self):
        clock=[100.0];posts=[]
        def slot(timeout):clock[0]+=2
        def post(*args,**kwargs):posts.append(kwargs);return self.response()
        analyzer=self.analyzer(SimpleNamespace(wait_http_slot=slot,succeeded=lambda:None),post)
        with patch('real_estate_engine.competitors.time.monotonic',side_effect=lambda:clock[0]):
            analyzer._call_nvidia_sync('Controlled queue wait',timeout=3)
        self.assertAlmostEqual(posts[0]['timeout'],1)


if __name__=='__main__':unittest.main()
