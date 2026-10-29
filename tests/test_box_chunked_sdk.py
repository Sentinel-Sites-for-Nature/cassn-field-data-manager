"""Exercise the installed SDK + upload transport + coordinator with fake HTTP."""
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import pytest
import requests
from box_sdk_gen import BoxClient
from box_sdk_gen.networking.box_network_client import BoxNetworkClient
from box_sdk_gen.networking.network import NetworkSession

from cassn.box.chunked import ChunkUploader, SDKChunkBackend
from cassn.box.transfer import UploadNetworkClient, UploadRuntime


class HTTPBox:
    def __init__(self):
        self.lock=threading.Lock()
        self.parts={}
        self.calls=[]
        self.pending=True
        self.lost=True
        self.created=0
        self.content=None
        self.list_sha512=True

    def close(self): pass

    def request(self, **kwargs):
        method,path=kwargs['method'],urlsplit(kwargs['url']).path.removeprefix('/api')
        body=kwargs['data']
        if hasattr(body,'read'): body=body.read()
        payload=json.loads(body) if isinstance(body,str) and body else body
        params=kwargs.get('params') or {}
        headers={}
        with self.lock:
            self.calls.append((method,path))
            if path=='/2.0/users/me':
                result={'type':'user','id':'sdk-test-actor'}
            elif method=='POST' and path=='/2.0/files/upload_sessions':
                self.created+=1
                self.name,self.size,self.parent=payload['file_name'],payload['file_size'],payload['folder_id']
                prefix='https://upload.box.com/2.0/files/upload_sessions/s1'
                result={'type':'upload_session','id':'s1','part_size':8,'total_parts':(self.size+7)//8,
                        'num_parts_processed':0,'session_expires_at':'2099-01-01T00:00:00Z',
                        'session_endpoints':{'upload_part':prefix,'list_parts':prefix+'/parts',
                                             'commit':prefix+'/commit','status':prefix}}
            elif method=='PUT':
                start=int(kwargs['headers']['content-range'].split(' ')[1].split('-')[0])
                assert start not in self.parts
                sha1=hashlib.sha1(payload).hexdigest()
                self.parts[start]=(payload,{'part_id':str(start),'offset':start,'size':len(payload),'sha1':sha1})
                if self.lost and start==0:
                    self.lost=False
                    raise requests.ConnectionError('synthetic lost part acknowledgement')
                result={'part':self.parts[start][1]}
            elif method=='GET' and path.endswith('/parts'):
                entries=[dict(r,sha1=None,sha512=hashlib.sha512(data).hexdigest())
                         if self.list_sha512 else r for data,r in self.parts.values()]
                result={'entries':entries, 'total_count':(self.size+7)//8,
                        'offset':int(params.get('offset',0)),'limit':1000}
            elif method=='POST' and path.endswith('/commit'):
                assert [p['offset'] for p in payload['parts']]==sorted(self.parts)
                if self.pending:
                    self.pending=False
                    headers={'retry-after':'120'}
                    response=requests.Response()
                    response.status_code=202
                    response.headers.update(headers)
                    response.url=kwargs['url']
                    response._content=b'{}'
                    return response
                self.content=b''.join(self.parts[o][0] for o in sorted(self.parts))
                result={'entries':[{'type':'file','id':'file1'}]}
            elif method=='GET' and path=='/2.0/files/file1':
                result={'type':'file','id':'file1','name':self.name,'parent':{'type':'folder','id':self.parent},
                        'size':len(self.content),'sha1':hashlib.sha1(self.content).hexdigest()}
            else:
                raise AssertionError((method,path))
        response=requests.Response()
        response.status_code=201 if method=='POST' else 200
        response.headers.update(headers)
        response.url=kwargs['url']
        response._content=json.dumps(result).encode()
        return response


@pytest.mark.parametrize('list_sha512',[False,True])
def test_actual_sdk_recovers_lost_part_and_pending_commit(tmp_path,monkeypatch,list_sha512):
    monkeypatch.setattr('box_sdk_gen.networking.box_network_client.time.sleep',
                        lambda delay:pytest.fail('hidden SDK retry sleep'))
    path=tmp_path/'source.wav'
    path.write_bytes(bytes(range(33)))
    waits=[]
    runtime=UploadRuntime(part_workers=2,transfers=2)
    runtime.bind_actor('sdk-test-actor')
    runtime.wait=lambda delay:waits.append(delay) or False
    http=HTTPBox()
    http.list_sha512=list_sha512
    network=UploadNetworkClient(runtime,client_factory=lambda:BoxNetworkClient(http))
    auth=NS(retrieve_authorization_header=lambda **kwargs:'Bearer synthetic')
    client=BoxClient(auth,network_session=NetworkSession(network_client=network))
    uploader=ChunkUploader(SDKChunkBackend(client),runtime,tmp_path/'qc/box_upload_sessions')
    assert uploader.upload(path,'parent','source.wav')=='file1'
    assert http.content==path.read_bytes()
    assert http.created==1
    assert waits==[120]
    assert len([c for c in http.calls if c[0]=='PUT'])==5
    snapshot=runtime.metrics.snapshot()
    assert snapshot['peak_active_requests']<=2
    assert snapshot['acknowledged_bytes']==33
    assert snapshot['attempted_payload_bytes']==33
    assert snapshot['status_202']==1
    network.close()


def test_one_connection_budget_covers_parallel_files_and_their_parts(tmp_path):
    runtime=UploadRuntime(transfers=2,part_workers=2)
    active=peak=0
    lock=threading.Lock()
    rendezvous=threading.Barrier(2)
    class Inner:
        def fetch(self,options):
            nonlocal active,peak
            with lock:
                active+=1
                peak=max(peak,active)
            rendezvous.wait(timeout=3)
            with lock: active-=1
            return NS(status=200)
    network=UploadNetworkClient(runtime,client_factory=Inner)
    session=NetworkSession(network_client=network)
    from box_sdk_gen.networking.fetch_options import FetchOptions
    def send(n):
        return network.fetch(FetchOptions(url='https://upload.box.com/2.0/files/upload_sessions/s',
                             method='PUT', file_stream=io.BytesIO(b'abc'),
                             content_type='application/octet-stream',network_session=session))
    with ThreadPoolExecutor(8) as pool:
        assert all(f.result().status==200 for f in [pool.submit(send,n) for n in range(16)])
    assert peak==2
    assert runtime.metrics.snapshot()['peak_active_requests']==2


def test_upload_client_clone_preserves_auth_and_binds_real_sdk_account(monkeypatch):
    from cassn.box import transfer
    http=HTTPBox()
    monkeypatch.setattr(transfer,'BoxNetworkClient',lambda:BoxNetworkClient(http))
    auth=NS(retrieve_authorization_header=lambda **kwargs:'Bearer synthetic')
    original=BoxClient(auth)
    runtime=UploadRuntime()
    clone,network=transfer.prepare_upload_client(original,runtime)
    assert clone is not original and clone.auth is original.auth
    assert original.network_session.network_client is not network
    assert runtime.actor=='sdk-test-actor'
    assert http.calls==[('GET','/2.0/users/me')]
    network.close()
