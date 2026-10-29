import pytest
from cassn.ndp.source_partials import preserve_orphan_partials


def test_only_known_inactive_temporaries_move_and_original_bytes_stay(tmp_path):
    root=tmp_path/'stage';folder=root/'raw_data/p4_ML';folder.mkdir(parents=True)
    original=folder/'a.jpg';original.write_bytes(b'original')
    partial=folder/'.a.jpg.1234abcd.partial';partial.write_bytes(b'unfinished')
    unknown=folder/'.unknown.jpg.1234abcd.partial';unknown.write_bytes(b'unknown')
    files=[dict(relative_path='raw_data/p4_ML/a.jpg',filename='a.jpg')]
    result=preserve_orphan_partials(root,files,tmp_path/'evidence')
    assert len(result)==1 and not partial.exists()
    assert __import__('pathlib').Path(result[0]['preserved_copy']).read_bytes()==b'unfinished'
    assert original.read_bytes()==b'original' and unknown.read_bytes()==b'unknown'


def test_active_temporary_is_never_moved(tmp_path):
    root=tmp_path/'stage';folder=root/'raw_data/p4_ML';folder.mkdir(parents=True)
    partial=folder/'.a.jpg.1234abcd.partial';partial.write_bytes(b'writing')
    files=[dict(relative_path='raw_data/p4_ML/a.jpg',filename='a.jpg')]
    with pytest.raises(ValueError,match='active'):
        preserve_orphan_partials(root,files,tmp_path/'evidence',active_filenames={'a.jpg'})
    assert partial.read_bytes()==b'writing'
