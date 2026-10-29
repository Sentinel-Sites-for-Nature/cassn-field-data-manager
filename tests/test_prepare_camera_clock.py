"""Standalone correction preserves originals and survives install interruptions."""
import json
from datetime import datetime,timedelta
from pathlib import Path

import pytest

from utils import prepare_camera_clock as prep


class FakeMetadata:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,path):return json.loads(path.read_text())['tags']
    def shift(self,path,seconds):
        data=json.loads(path.read_text());data['tags']=prep.shifted(data['tags'],seconds)
        data['changes']+=1;path.write_text(json.dumps(data))


@pytest.fixture
def package(tmp_path):
    source=tmp_path/'card';(source/'DCIM/nested').mkdir(parents=True)
    for relative in ['DCIM/one.jpg','DCIM/nested/two.JPG']:
        (source/relative).write_text(json.dumps({'tags':{'ExifIFD:DateTimeOriginal':'2026:09:05 11:14:07',
           'IFD0:ModifyDate':'2026:09:05 11:14:07','Reconyx:DateTimeOriginal':'2026:09:05 11:14:07',
           'ExifIFD:OffsetTimeOriginal':'-08:00','Reconyx:SerialNumber':'09022966'},'changes':0}))
    (source/'CONFIG.TXT').write_text('preserve me')
    output=tmp_path/'prepared'
    before={p.relative_to(source).as_posix():p.read_bytes() for p in source.rglob('*') if p.is_file()}
    def run(**kw):prep.execute(source,output,-4*86400,metadata_factory=FakeMetadata,**kw)
    return source,output,before,run


def assert_complete(package):
    source,output,before,_=package
    assert json.loads((output/'manifest.json').read_text())['status']=='complete'
    for relative,data in before.items():
        assert (source/relative).read_bytes()==data
        assert (output/'originals'/relative).read_bytes()==data
        new=output/'ready'/relative
        if new.suffix.lower()=='.jpg':
            d=json.loads(new.read_text());assert d['changes']==1
            assert d['tags']['ExifIFD:DateTimeOriginal']=='2026:09:01 11:14:07'
            assert d['tags']['Reconyx:DateTimeOriginal']=='2026:09:01 11:14:07'
            assert d['tags']['ExifIFD:OffsetTimeOriginal']=='-08:00'
        else:assert new.read_bytes()==data


def test_preview_writes_nothing_and_apply_retry_does_not_shift_twice(package):
    source,output,before,run=package
    run(dry_run=True);assert not output.exists()
    run();assert_complete(package)
    hashes={p:prep.digest(p) for p in (output/'ready').rglob('*') if p.is_file()}
    run();assert_complete(package)
    assert hashes=={p:prep.digest(p) for p in hashes}


@pytest.mark.parametrize('after',[False,True])
def test_install_interruption_recovers_from_receipt(package,monkeypatch,after):
    _,output,_,run=package;real=prep.install;fired=False
    def install(src,dst):
        nonlocal fired
        if 'working' in dst.parts and not fired:
            fired=True
            if after:real(src,dst)
            raise OSError('interrupted install')
        real(src,dst)
    monkeypatch.setattr(prep,'install',install)
    with pytest.raises(OSError):run()
    assert not (output/'ready').exists()
    monkeypatch.setattr(prep,'install',real);run();assert_complete(package)


def test_partial_exiftool_write_is_not_published(package,monkeypatch):
    _,output,_,run=package;real=FakeMetadata.shift
    def fail(self,path,seconds):real(self,path,seconds);raise OSError('partial edit')
    monkeypatch.setattr(FakeMetadata,'shift',fail)
    with pytest.raises(OSError):run()
    assert not (output/'ready').exists()
    monkeypatch.setattr(FakeMetadata,'shift',real);run();assert_complete(package)


def test_changed_source_blocks_retry(package):
    source,output,_,run=package;run()
    (source/'DCIM/one.jpg').write_text('changed')
    with pytest.raises((ValueError,json.JSONDecodeError)):run()


def test_changed_corrected_copy_is_preserved_and_rejected(package):
    _,output,_,run=package;run();path=output/'ready/DCIM/one.jpg';path.write_text('unknown')
    with pytest.raises(ValueError,match='evidence differs'):run()
    assert path.read_text()=='unknown'


def test_completion_marker_failure_does_not_double_shift(package,monkeypatch):
    _,output,_,run=package;real=prep.save
    def save(path,value):
        if value.get('status')=='complete':raise OSError('crash after ready rename')
        real(path,value)
    monkeypatch.setattr(prep,'save',save)
    with pytest.raises(OSError):run()
    monkeypatch.setattr(prep,'save',real);run();assert_complete(package)


def test_unsafe_or_existing_output_is_rejected(package,tmp_path):
    source,output,_,run=package
    with pytest.raises(ValueError,match='separate'):prep.execute(source,source/'inside',1,metadata_factory=FakeMetadata)
    output.mkdir();(output/'valuable').write_text('keep')
    with pytest.raises(ValueError,match='already exists'):run()
    assert (output/'valuable').read_text()=='keep'


def test_symlink_source_is_rejected(package,tmp_path):
    source,_,_,run=package;(source/'linked.jpg').symlink_to(source/'DCIM/one.jpg')
    with pytest.raises(ValueError,match='Symlink'):run()


@pytest.mark.parametrize('dry_run', [False, True])
def test_unreadable_source_folder_stops_before_output(package, monkeypatch, dry_run):
    source, output, before, run = package
    real_scandir = prep.os.scandir

    def unreadable(path):
        if Path(path) == source / 'DCIM/nested':
            raise PermissionError('Source folder is unreadable')
        return real_scandir(path)

    monkeypatch.setattr(prep.os, 'scandir', unreadable)
    with pytest.raises(PermissionError, match='unreadable'):
        run(dry_run=dry_run)
    assert not output.exists()
    for relative, data in before.items():
        assert (source / relative).read_bytes() == data


def test_mismatched_retry_request_is_rejected(package):
    source,output,_,run=package;run()
    with pytest.raises(ValueError,match='original source'):prep.execute(source,output,86400,metadata_factory=FakeMetadata)


@pytest.mark.integration
def test_real_jpeg_exif_shift_preserves_pixels_offsets_and_source(tmp_path):
    from PIL import Image
    import piexif
    source=tmp_path/'card';source.mkdir();image=source/'test.jpg'
    exif={'0th':{piexif.ImageIFD.DateTime:b'2026:09:05 11:14:07'},
          'Exif':{piexif.ExifIFD.DateTimeOriginal:b'2026:09:05 11:14:07',
                  piexif.ExifIFD.DateTimeDigitized:b'2026:09:05 11:14:07',36881:b'-08:00'},'GPS':{},'1st':{},'thumbnail':None}
    Image.new('RGB',(16,16),(100,20,30)).save(image,exif=piexif.dump(exif))
    before=image.read_bytes();pixels=Image.open(image).tobytes();output=tmp_path/'prepared'
    prep.execute(source,output,-4*86400)
    assert image.read_bytes()==before
    assert Image.open(output/'ready/test.jpg').tobytes()==pixels
    with prep.Metadata() as metadata:
        old=metadata.read(image);new=metadata.read(output/'ready/test.jpg')
        assert new==prep.shifted(old,-4*86400)
    prep.execute(source,output,-4*86400)
