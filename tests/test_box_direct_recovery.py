"""Direct-file lost replies must not multiply uploads or sidecar versions."""
from pathlib import Path
import requests
import pytest
from cassn.box.client import BoxStorage
from cassn.box.transfer import UploadRuntime, UploadPaused
from test_box_metadata_publication import FakeBox


def test_lost_new_file_response_is_reconciled(tmp_path):
    api=FakeBox()
    path=tmp_path/'image.jpg'
    path.write_bytes(b'image')
    api.after_write=lambda fid:(_ for _ in ()).throw(requests.ConnectionError('lost reply'))
    runtime=UploadRuntime()
    runtime.wait=lambda delay:False
    storage=BoxStorage(api,upload_runtime=runtime)
    assert storage.upload_file_with_path(path,'event',Path('image.jpg'))=='uploaded'
    assert len(api.calls)==1
    assert api.data('image.jpg')==b'image'
    assert runtime.metrics.snapshot()['acknowledged_bytes']==5


def test_new_file_retry_rewinds_after_partial_send(tmp_path):
    api=FakeBox()
    path=tmp_path/'image.jpg'
    path.write_bytes(b'image')
    original=api.upload_file
    calls=[]
    def upload(**kwargs):
        calls.append(kwargs['file'].tell())
        if len(calls)==1:
            kwargs['file'].read(2)
            raise requests.ConnectionError('before acceptance')
        return original(**kwargs)
    api.upload_file=upload
    runtime=UploadRuntime()
    runtime.wait=lambda delay:False
    storage=BoxStorage(api,upload_runtime=runtime)
    assert storage.upload_file_with_path(path,'event',Path('image.jpg'))=='uploaded'
    assert calls==[0,0]
    assert api.data('image.jpg')==b'image'


def test_uncertain_version_is_checked_without_making_another_version(tmp_path):
    api=FakeBox()
    fid=api.add('sidecar.txt',b'old')
    path=tmp_path/'sidecar.txt'
    path.write_bytes(b'new')
    calls=[]
    def upload(file_id,attributes,file):
        calls.append(file_id)
        api.change(file_id,file.read())
        raise requests.ConnectionError('lost version reply')
    api.upload_file_version=upload
    storage=BoxStorage(api,upload_runtime=UploadRuntime())
    assert storage.upload_file_with_path(path,'event',Path(path.name))=='versioned'
    assert calls==[fid] and api.objects[fid]['version']==2


def test_mismatched_remote_copy_never_counts_as_recovered(tmp_path):
    api=FakeBox()
    path=tmp_path/'image.jpg'
    path.write_bytes(b'image')
    def upload(**kwargs):
        api.add('image.jpg',b'wrong')
        raise requests.ConnectionError('lost')
    api.upload_file=upload
    storage=BoxStorage(api,upload_runtime=UploadRuntime())
    with pytest.raises(UploadPaused,match='does not match'):
        storage.upload_file_with_path(path,'event',Path('image.jpg'))
