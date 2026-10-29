"""Failure-injected chunk sessions; synthetic bytes, no Box/network access."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from types import SimpleNamespace as NS

import pytest
import requests

from cassn.box.chunked import ChunkUploader, SDKChunkBackend
from cassn.box.client import BoxStorage
from cassn.box.transfer import UploadCancelled, UploadPaused, UploadRuntime
from cassn.box.upload_state import atomic_json, require_uploads_complete


def fail(status):
    exc = RuntimeError(f'HTTP {status}')
    exc.response_info = NS(status_code=status, headers={})
    return exc


class Backend:
    def __init__(self):
        self.created=0
        self.sent=[]
        self.remote={}
        self.commits=[]
        self.file=None
        self.session=None
        self.lost_parts=set()
        self.failed_parts=Counter()
        self.pending=0
        self.lost_commit=False
        self.lock=threading.Lock()
        self.active=self.peak=0
        self.after_part=None
        self.before_part=None
        self.expired=False

    def create(self,parent,size,name):
        self.created+=1
        self.session=NS(id='session-1',part_size=8,total_parts=(size+7)//8,
                        session_expires_at=datetime.now(timezone.utc)+timedelta(days=7))
        self.parent,self.size,self.name=parent,size,name
        return self.session

    def status(self,sid):
        assert sid==self.session.id
        if self.expired or self.file: raise fail(404)
        return self.session

    def parts(self,session):
        with self.lock:
            return [dict(r) for _,r in sorted(self.remote.items())]

    def put(self,session,offset,data,total):
        assert total==self.size
        with self.lock:
            self.active+=1
            self.peak=max(self.peak,self.active)
            self.sent.append(offset)
        try:
            if self.before_part: self.before_part(offset)
            if self.failed_parts[offset]:
                self.failed_parts[offset]-=1
                raise requests.ConnectionError('before receipt')
            receipt=dict(part_id=f'part-{offset}',offset=offset,size=len(data),sha1=hashlib.sha1(data).hexdigest())
            with self.lock:
                assert offset not in self.remote, 'attempted to overwrite an acknowledged part'
                self.remote[offset]=receipt
            if self.after_part: self.after_part(offset)
            if offset in self.lost_parts:
                self.lost_parts.remove(offset)
                raise requests.ConnectionError('lost acknowledgement')
            return receipt
        finally:
            with self.lock: self.active-=1

    def commit(self,session,receipts,sha1):
        self.commits.append((session.id,list(receipts),sha1))
        assert [r['offset'] for r in receipts]==sorted(self.remote)
        assert receipts==[self.remote[o] for o in sorted(self.remote)]
        assert sum(r['size'] for r in receipts)==self.size
        if self.pending:
            self.pending-=1
            return NS(status=202,headers={'retry-after':'120'},data={})
        self.file='file-1'
        if self.lost_commit:
            self.lost_commit=False
            raise requests.ConnectionError('lost commit reply')
        return NS(status=201,headers={},data={'entries':[{'id':self.file}]})

    def find_file(self,parent,name,size,sha1):
        return self.verify_file(self.file,parent,name,size,sha1) if self.file else None

    def verify_file(self,file_id,parent,name,size,sha1):
        assert file_id==self.file and parent==self.parent and name==self.name and size==self.size
        assert sha1==self.expected_sha1
        return file_id


def prepared(tmp_path, *, workers=2, backend=None):
    path=tmp_path/'source.wav'
    path.write_bytes(b'abcdefgh01234567ABCDEFGHabcdefgh!' ) # 33 bytes, short final part
    api=backend or Backend()
    api.expected_sha1=hashlib.sha1(path.read_bytes()).hexdigest()
    runtime=UploadRuntime(part_workers=workers)
    runtime.bind_actor('test-chunks-' + tmp_path.name)
    runtime.wait=lambda seconds:False
    journals=tmp_path/'qc/box_upload_sessions'
    return path,api,runtime,ChunkUploader(api,runtime,journals)


def state(uploader):
    paths=list(uploader.journal_dir.glob('*.json'))
    assert len(paths)==1
    return json.loads(paths[0].read_text())


@pytest.mark.parametrize('workers',[1,2,4])
def test_parallel_parts_short_tail_hash_and_sorted_commit(tmp_path,workers):
    path,api,runtime,uploader=prepared(tmp_path,workers=workers)
    assert uploader.upload(path,'parent','source.wav')=='file-1'
    assert api.created==1
    assert api.peak<=workers
    assert sorted(api.sent)==[0,8,16,24,32]
    assert state(uploader)['state']=='complete'
    assert state(uploader)['identity']['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert runtime.buffer_used==0
    assert runtime.metrics.snapshot()['acknowledged_bytes']==path.stat().st_size
    require_uploads_complete(tmp_path)


def test_out_of_order_completion_keeps_source_hash_and_commit_order(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    later=threading.Event()
    def before(offset):
        if offset==0: assert later.wait(2)
        elif offset==8: later.set()
    api.before_part=before
    assert uploader.upload(path,'parent','source.wav')=='file-1'
    assert api.peak==2
    assert [p['offset'] for p in api.commits[-1][1]]==[0,8,16,24,32]


def test_lost_part_reply_reconciles_without_resending(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    api.lost_parts={0,16}
    uploader.upload(path,'parent','source.wav')
    assert Counter(api.sent)==Counter([0,8,16,24,32])
    assert api.created==1


def test_part_not_received_retries_only_that_part(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    api.failed_parts[8]=1
    uploader.upload(path,'parent','source.wav')
    assert Counter(api.sent)==Counter([0,8,8,16,24,32])
    assert api.created==1


def test_pending_commits_wait_and_reuse_same_session(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    waits=[]
    runtime.wait=lambda seconds:waits.append(seconds) or False
    api.pending=2
    uploader.upload(path,'parent','source.wav')
    assert waits==[120,120]
    assert len(api.commits)==3
    assert {s for s,_,_ in api.commits}=={'session-1'}
    assert api.created==1


def test_twenty_pending_commits_remain_recoverable(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    api.pending=20
    with pytest.raises(UploadPaused, match='still processing'): uploader.upload(path,'parent','source.wav')
    assert state(uploader)['state']=='committing'
    with pytest.raises(ValueError, match='Retain staging'): require_uploads_complete(tmp_path)
    uploader.upload(path,'parent','source.wav')
    assert api.created==1 and len(api.sent)==5


def test_lost_commit_response_reconciles_exact_file(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    api.lost_commit=True
    assert uploader.upload(path,'parent','source.wav')=='file-1'
    assert api.created==1 and len(api.commits)==1


def test_cancel_then_resume_uses_acknowledged_parts(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    api.after_part=lambda offset:runtime.cancel.set() if offset==0 else None
    with pytest.raises(UploadCancelled): uploader.upload(path,'parent','source.wav')
    assert state(uploader)['state']=='uploading'
    with pytest.raises(ValueError): require_uploads_complete(tmp_path)
    runtime.cancel.clear()
    api.after_part=None
    assert uploader.upload(path,'parent','source.wav')=='file-1'
    assert api.sent.count(0)==1 and api.created==1
    assert runtime.buffer_used==0


def test_changed_source_refuses_resume(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    api.after_part=lambda offset:runtime.cancel.set()
    with pytest.raises(UploadCancelled): uploader.upload(path,'parent','source.wav')
    runtime.cancel.clear()
    path.write_bytes(b'changed!'+path.read_bytes()[8:])
    with pytest.raises(UploadPaused, match='changed source'): uploader.upload(path,'parent','source.wav')
    assert not api.commits and api.created==1


def test_source_changes_during_transfer_prevent_commit(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    def change(offset):
        if offset==0: path.write_bytes(b'changed!'+path.read_bytes()[8:])
    api.after_part=change
    with pytest.raises(UploadPaused,match='Source changed'): uploader.upload(path,'parent','source.wav')
    assert not api.commits and state(uploader)['state']=='needs_review'


def test_wrong_part_hash_stops_before_commit(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    original=api.put
    def wrong(*args):
        receipt=original(*args)
        receipt['sha1']='0'*40
        return receipt
    api.put=wrong
    with pytest.raises(UploadPaused,match='receipt'): uploader.upload(path,'parent','source.wav')
    assert not api.commits and runtime.buffer_used==0


def test_resume_sha1_journal_with_sha512_listing_without_resending(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    original=api.parts
    calls=0
    def pause_final(session):
        nonlocal calls
        calls+=1
        if calls==2:
            raise UploadPaused('synthetic final listing interruption')
        return original(session)
    api.parts=pause_final
    with pytest.raises(UploadPaused,match='synthetic'):
        uploader.upload(path,'parent','source.wav')
    # Simulate recovery records written by the previous SHA-1-only app.
    journal=next(uploader.journal_dir.glob('*.json'))
    saved=json.loads(journal.read_text())
    for r in saved['receipts'].values(): r.pop('sha512',None)
    atomic_json(journal,saved)
    data=path.read_bytes()
    def sha512_parts(session):
        return [dict(r,sha1=None,sha512=hashlib.sha512(
            data[r['offset']:r['offset']+r['size']]).hexdigest()) for r in original(session)]
    api.parts=sha512_parts
    assert uploader.upload(path,'parent','source.wav')=='file-1'
    assert len(api.sent)==5 and api.created==1


@pytest.mark.parametrize('bad',['hash','identity','missing_hash'])
def test_final_listing_mismatch_still_prevents_commit(tmp_path,bad):
    path,api,runtime,uploader=prepared(tmp_path)
    original=api.parts
    def altered(session):
        parts=original(session)
        if parts:
            if bad=='hash': parts[0].update(sha1=None,sha512='0'*128)
            elif bad=='identity': parts[0]['part_id']='different'
            else: parts[0].update(sha1=None)
        return parts
    api.parts=altered
    with pytest.raises(UploadPaused,match='not confirmed'):
        uploader.upload(path,'parent','source.wav')
    assert not api.commits


def test_expired_session_pauses_instead_of_restarting(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    api.after_part=lambda offset:runtime.cancel.set()
    with pytest.raises(UploadCancelled): uploader.upload(path,'parent','source.wav')
    runtime.cancel.clear()
    api.expired=True
    with pytest.raises(UploadPaused,match='expired'): uploader.upload(path,'parent','source.wav')
    assert api.created==1


@pytest.mark.parametrize('boundary',['create','receipt','committing','complete'])
def test_restart_across_journal_network_boundaries(tmp_path,monkeypatch,boundary):
    from cassn.box import chunked
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    fired=False
    def interrupted(journal,data):
        nonlocal fired
        trigger={'create':data.get('session_id') is not None and not data.get('receipts'),
                 'receipt':bool(data.get('receipts')) and data.get('state')=='uploading',
                 'committing':data.get('state')=='committing',
                 'complete':data.get('state')=='complete'}[boundary]
        if trigger and not fired:
            fired=True
            raise OSError('simulated interruption before journal replacement')
        atomic_json(journal,data)
    monkeypatch.setattr(chunked,'atomic_json',interrupted)
    with pytest.raises(OSError): uploader.upload(path,'parent','source.wav')
    monkeypatch.setattr(chunked,'atomic_json',atomic_json)
    # A newly constructed coordinator models process restart; same files/account.
    resumed=ChunkUploader(api,runtime,uploader.journal_dir)
    if boundary=='create':
        with pytest.raises(UploadPaused,match='creation outcome'): resumed.upload(path,'parent','source.wav')
    else:
        assert resumed.upload(path,'parent','source.wav')=='file-1'
        assert max(Counter(api.sent).values())==1
    assert api.created==1


def test_overlapping_remote_parts_are_rejected(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    api.parts=lambda session:[dict(part_id='x',offset=1,size=8,sha1='0'*40)]
    with pytest.raises(UploadPaused,match='coverage'): uploader.upload(path,'parent','source.wav')
    assert not api.sent


def test_no_cross_account_reuse_and_foreign_record_is_rejected(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=1)
    api.after_part=lambda offset:runtime.cancel.set()
    with pytest.raises(UploadCancelled): uploader.upload(path,'parent','source.wav')
    record=next(uploader.journal_dir.glob('*.json'))
    data=json.loads(record.read_text())
    data['identity']['actor']='someone-else'
    atomic_json(record,data)
    runtime.cancel.clear()
    with pytest.raises(UploadPaused,match='changed source or destination'): uploader.upload(path,'parent','source.wav')
    other=UploadRuntime()
    other.bind_actor('another-account')
    other_uploader=ChunkUploader(api,other,uploader.journal_dir)
    assert uploader._key(path,'parent','source.wav')!=other_uploader._key(path,'parent','source.wav')


def test_concurrent_process_lock_and_journal_exclusion(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path)
    key=uploader._key(path,'parent','source.wav')
    with uploader._locked(key):
        with pytest.raises(UploadPaused,match='another process'):
            with uploader._locked(key): pass
    for name in ['abc.json','abc.lock','.abc.tmp','metrics.json']:
        assert not BoxStorage.should_upload_file(tmp_path/'qc/box_upload_sessions'/name)


def test_sdk_part_listing_paginates_and_pending_commit_keeps_headers():
    calls=[]
    class Manager:
        auth=None
        def get_file_upload_session_parts_by_url(self,url,**kwargs):
            calls.append(kwargs)
            items=range(kwargs['offset'],min(1001,kwargs['offset']+kwargs['limit']))
            return NS(entries=[NS(part_id=str(i),offset=i*8,size=8,sha_1='0'*40) for i in items],total_count=1001)
    response=NS(status=202,headers={'retry-after':'120'})
    seen=[]
    manager=Manager()
    manager.network_session=NS(network_client=NS(fetch=lambda options:seen.append(options) or response))
    backend=SDKChunkBackend(NS(chunked_uploads=manager))
    session=NS(session_endpoints=NS(list_parts='https://upload.box.com/parts',commit='https://upload.box.com/commit'))
    assert len(list(backend.parts(session)))==1001
    assert [c['offset'] for c in calls]==[0,1000]
    assert backend.commit(session,[],'0'*40) is response
    assert seen[0].headers['digest'].startswith('sha=')
    assert seen[0].data=={'parts':[]}


def test_buffer_budget_is_shared_across_parallel_files(tmp_path):
    path,api,runtime,uploader=prepared(tmp_path,workers=4)
    runtime.buffer_limit=8
    assert uploader.upload(path,'parent','source.wav')=='file-1'
    assert runtime.buffer_used==0 and api.peak<=1


def test_sdk_parts_handles_sparse_and_empty_pages_using_returned_limit():
    calls=[]
    part=NS(part_id='last',offset=16,size=8,sha_1='0'*40)
    class Manager:
        def get_file_upload_session_parts_by_url(self,url,**kwargs):
            offset=kwargs['offset']
            calls.append(offset)
            # Box can return fewer entries than total_count, including empty
            # pages, and cap the requested limit.
            return NS(entries=[part] if offset==4 else [],total_count=5,limit=2)
    backend=SDKChunkBackend(NS(chunked_uploads=Manager()))
    session=NS(session_endpoints=NS(list_parts='https://upload.box.com/parts'))
    assert [r['part_id'] for r in backend.parts(session)]==['last']
    assert calls==[0,2,4]


def test_sdk_parts_accepts_empty_new_session_with_positive_total_count():
    class Manager:
        def get_file_upload_session_parts_by_url(self,url,**kwargs):
            assert kwargs['offset']==0
            return NS(entries=[],total_count=5,limit=1000)
    backend=SDKChunkBackend(NS(chunked_uploads=Manager()))
    session=NS(session_endpoints=NS(list_parts='https://upload.box.com/parts'))
    assert list(backend.parts(session))==[]


@pytest.mark.parametrize('force',[False,True])
def test_pending_chunk_blocks_staging_cleanup_even_with_old_pass(tmp_path,force):
    from test_staging_cleanup import _build_event
    from cassn.core.staging_cleanup import inspect_deployment_for_cleanup
    staging,event,storage=_build_event(tmp_path)
    journal=event/'qc/box_upload_sessions/pending.json'
    atomic_json(journal,{'version':1,'state':'committing','session_id':'pending'})
    plan=inspect_deployment_for_cleanup(event,staging,storage,'field-root',force=force)
    assert not plan.clearable
    assert any('recovery record' in reason for reason in plan.reasons)
