"""Opt-in WSGI candidate. Current route/UI remains owned by frontend."""
import json,threading,time
from .automation_status import sync,frontend_view

class StatusMiddleware:
    def __init__(self,application,reader,cache,revision_provider,poll_seconds=30):
        self.application=application;self.reader=reader;self.cache=cache;self.revision_provider=revision_provider
        self.poll_seconds=poll_seconds;self.last_poll=None;self.guard=threading.Lock()
    def __call__(self,environ,start_response):
        if environ.get('PATH_INFO')!='/api/automation-status':return self.application(environ,start_response)
        if environ.get('REQUEST_METHOD')!='GET':
            start_response('405 Method Not Allowed',[('Content-Type','application/json'),('Cache-Control','no-store')]);return [b'{"available":false}']
        with self.guard:
            try:
                revision=self.revision_provider()
                if self.last_poll is None or time.monotonic()-self.last_poll>=self.poll_seconds:
                    sync(self.reader,self.cache,revision);self.last_poll=time.monotonic()
                # A concurrent financial reader may have switched while the network call ran.
                payload=frontend_view(self.cache,self.revision_provider())
            except Exception:payload=dict(version=1,available=False,channels={},run_status='failed',last_success=None)
        raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
        start_response('200 OK',[('Content-Type','application/json; charset=utf-8'),('Cache-Control','no-store'),('Content-Length',str(len(raw)))]);return [raw]
