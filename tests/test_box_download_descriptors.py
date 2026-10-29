"""Descriptor ownership across concurrent Box downloads, without network access."""
import errno
import hashlib
import io
import os
from types import SimpleNamespace

import pytest

from cassn.ndp import box_download
from cassn.ndp.transfer import TransferFile


def source():
    return TransferFile('id', 'one.jpg', 'one.jpg', 3, hashlib.sha256(b'abc').hexdigest())


def client(stream=None, error=None):
    def download(_):
        if error:
            raise error
        return stream or io.BytesIO(b'abc')
    return SimpleNamespace(downloads=SimpleNamespace(download_file=download))


def test_published_download_does_not_close_reused_descriptor(tmp_path, monkeypatch):
    replace = os.replace
    guard = []
    def reuse_after_output_closed(src, dest):
        guard.append(os.open(tmp_path / 'unrelated', os.O_CREAT | os.O_RDWR, 0o600))
        replace(src, dest)
    monkeypatch.setattr(box_download.os, 'replace', reuse_after_output_closed)
    try:
        box_download.download_box_file(client(), source(), tmp_path / 'one.jpg')
        os.fstat(guard[0])
        assert (tmp_path / 'one.jpg').read_bytes() == b'abc'
    finally:
        for fd in guard:
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize('failure', ['download', 'read', 'cancel'])
def test_failure_closes_owned_output_and_removes_partial(tmp_path, monkeypatch, failure):
    created = []
    mkstemp = box_download.tempfile.mkstemp
    def track(*args, **kwargs):
        result = mkstemp(*args, **kwargs)
        created.append(result[0])
        return result
    monkeypatch.setattr(box_download.tempfile, 'mkstemp', track)
    class Broken(io.BytesIO):
        def read(self, *_):
            raise OSError(errno.EIO, 'sensitive signed URL must not be logged')
    stream = Broken(b'abc') if failure == 'read' else io.BytesIO(b'abc')
    c = client(stream, OSError(errno.EIO, 'download failed') if failure == 'download' else None)
    with pytest.raises((OSError, box_download.TransferCancelled)):
        box_download.download_box_file(c, source(), tmp_path / 'one.jpg',
                                      is_cancelled=lambda: failure == 'cancel')
    with pytest.raises(OSError) as error:
        os.fstat(created[0])
    assert error.value.errno == errno.EBADF
    assert not list(tmp_path.glob('*.partial'))
    assert not (tmp_path / 'one.jpg').exists()


def test_oserror_context_contains_errno_but_no_remote_message(tmp_path):
    with pytest.raises(OSError) as caught:
        box_download.download_box_file(client(error=OSError(errno.EIO, 'secret capability')),
                                      source(), tmp_path / 'one.jpg')
    detail = caught.value.box_download_context
    assert detail == {'filename': 'one.jpg', 'operation': 'download',
                      'error_type': 'OSError', 'errno': errno.EIO,
                      'message': os.strerror(errno.EIO)}
    assert 'secret' not in str(detail)


def test_fdopen_failure_closes_descriptor_before_ownership_transfer(tmp_path, monkeypatch):
    descriptors = []
    def fail_open(fd, *_):
        descriptors.append(fd)
        raise OSError(errno.EMFILE, 'secret remote text')
    monkeypatch.setattr(box_download.os, 'fdopen', fail_open)
    with pytest.raises(OSError) as caught:
        box_download.download_box_file(client(), source(), tmp_path / 'one.jpg')
    assert caught.value.box_download_context['operation'] == 'open_temporary'
    assert caught.value.box_download_context['errno'] == errno.EMFILE
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not list(tmp_path.glob('*.partial'))


def test_changed_written_bytes_are_never_published(tmp_path, monkeypatch):
    fsync = os.fsync
    def corrupt_output(fd):
        os.pwrite(fd, b'xyz', 0)
        fsync(fd)
    monkeypatch.setattr(box_download.os, 'fsync', corrupt_output)
    with pytest.raises(box_download.NdpTransferError, match='written copy'):
        box_download.download_box_file(client(), source(), tmp_path / 'one.jpg')
    assert not (tmp_path / 'one.jpg').exists()
    assert not list(tmp_path.glob('*.partial'))


def test_completed_download_cannot_redirect_another_workers_bytes(tmp_path, monkeypatch):
    """Model A closing, B reusing its fd, then C opening before B writes."""
    replace = os.replace
    handles = []
    def start_next_worker(src, dest):
        handles.append(os.open(tmp_path / 'worker-B', os.O_CREAT | os.O_RDWR, 0o600))
        replace(src, dest)
    monkeypatch.setattr(box_download.os, 'replace', start_next_worker)
    try:
        box_download.download_box_file(client(), source(), tmp_path / 'one.jpg')
        handles.append(os.open(tmp_path / 'worker-C', os.O_CREAT | os.O_RDWR, 0o600))
        # With the original double-close, C reused B's accidentally closed fd,
        # and B's next write landed in C's file despite B hashing its own bytes.
        os.write(handles[0], b'xyz')
        assert (tmp_path / 'worker-B').read_bytes() == b'xyz'
        assert (tmp_path / 'worker-C').read_bytes() == b''
    finally:
        for fd in set(handles):
            try:
                os.close(fd)
            except OSError:
                pass
