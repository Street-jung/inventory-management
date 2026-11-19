"""Opt-in HTTP generation API candidate. No environment auto-activation."""
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
try:
    from . import cloud_generation as protocol
except ImportError:
    import cloud_generation as protocol


class RemoteError(RuntimeError):pass
class OutcomeUnknown(RemoteError):pass


class _ContentRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urlsplit(newurl)
        if code not in (302,303) or parsed.scheme!='https' or parsed.hostname!='script.googleusercontent.com' or parsed.username or parsed.password:
            raise RemoteError('unexpected generation redirect')
        # ContentService result retrieval is GET-only; never forward the
        # request body, token or authorization headers to another host.
        return Request(newurl,method='GET',headers={'Accept':'application/json'})


class HTTPGenerationTransport:
    def __init__(self,url,token,*,enabled=False,allow_loopback=False,timeout=15):
        parsed=urlsplit(url)
        local=allow_loopback and parsed.hostname in ('127.0.0.1','localhost','::1')
        if not enabled or not token or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('explicit endpoint, credential and opt-in required')
        if parsed.scheme!='https' and not (local and parsed.scheme=='http'):
            raise ValueError('HTTPS required')
        self.url=url;self._token=token;self.timeout=timeout

    def call(self,action,payload=None):
        body=json.dumps(dict(action=action,token=self._token,payload=payload or {}),ensure_ascii=False).encode()
        req=Request(self.url,data=body,headers={'Content-Type':'application/json'},method='POST')
        try:
            with build_opener(_ContentRedirect()).open(req,timeout=self.timeout) as response:
                result=json.load(response)
        except HTTPError as exc:
            if exc.code>=500:raise OutcomeUnknown('server outcome unavailable; query status before retry') from None
            raise RemoteError('generation HTTP request rejected') from None
        except Exception:
            raise OutcomeUnknown('generation response unavailable; query status before retry') from None
        if not isinstance(result,dict) or result.get('ok') is not True:
            code=result.get('code') if isinstance(result,dict) else None
            allowed={'unauthorized','forbidden','invalid','conflict','missing','regression','busy','internal'}
            raise RemoteError('generation API '+(code if code in allowed else 'invalid'))
        return result['data']

    def current(self):return self.call('generation_current')
    def status(self,revision):
        protocol.safe_revision(revision)
        return self.call('generation_status',dict(revision=revision))
    def get(self,revision,name):
        protocol.safe_revision(revision)
        result=self.call('generation_get',dict(revision=revision,name=name))
        try:
            blob=base64.b64decode(result['content_base64'],validate=True)
            if len(blob)!=result['bytes'] or hashlib.sha256(blob).hexdigest()!=result['sha256']:raise ValueError()
            return blob
        except Exception:raise RemoteError('invalid object integrity') from None
    def put(self,revision,name,blob):
        protocol.safe_revision(revision)
        try:
            return self.call('generation_put',dict(revision=revision,name=name,bytes=len(blob),
                sha256=hashlib.sha256(blob).hexdigest(),content_base64=base64.b64encode(blob).decode('ascii')))
        except OutcomeUnknown:
            try:stored=self.get(revision,name)
            except RemoteError:raise OutcomeUnknown('upload outcome unresolved; do not blindly replay') from None
            if stored!=blob:raise OutcomeUnknown('upload outcome mismatched') from None
            return dict(stored=True,confirmed_by_read=True)

    def switch(self,action,revision,expected_previous):
        try:
            return self.call(action,dict(revision=revision,expected_previous=expected_previous))
        except OutcomeUnknown:
            # Never blindly replay a mutation. An unconfirmed result remains
            # unknown even if the requested revision's objects exist.
            try:state=self.status(revision)
            except RemoteError:raise OutcomeUnknown('commit outcome unresolved') from None
            pointer=state.get('current') or {}
            if pointer.get('revision')==revision and pointer.get('rollback') is (action=='generation_rollback'):
                return pointer
            raise OutcomeUnknown('commit not confirmed; explicit operator reconciliation required') from None


def publish_remote(candidate,remote,expected_previous):
    files,meta=protocol.payload(Path(candidate));pointer=remote.current();old=(pointer or {}).get('revision')
    records={k:dict(bytes=len(v),sha256=hashlib.sha256(v).hexdigest()) for k,v in files.items()}
    if old:
        previous,_=protocol.verify_generation(remote,old)
        if previous['files']==records and all(previous[k]==v for k,v in meta.items()):return old
        protocol.require_no_regression(meta,previous)
    if old!=expected_previous:raise RemoteError('generation API conflict')
    manifest=dict(meta,files=records,previous_revision=old)
    raw=protocol.encoded(manifest);revision=hashlib.sha256(raw).hexdigest()
    for name,blob in files.items():remote.put(revision,name,blob)
    remote.put(revision,'manifest.json',raw)
    protocol.verify_generation(remote,revision)
    remote.switch('generation_commit',revision,old)
    return revision


def rollback_remote(remote,revision,expected_previous):
    protocol.verify_generation(remote,revision)
    return remote.switch('generation_rollback',revision,expected_previous)
