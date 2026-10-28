"""Preserve recognizable interrupted-download temporaries, never originals."""
import hashlib
import os
from pathlib import Path
import re


def preserve_orphan_partials(root, files, quarantine, *, active_filenames=()):
    root=Path(root).resolve();quarantine=Path(quarantine).resolve()
    if quarantine.is_relative_to(root):
        raise ValueError('Partial evidence must be outside source staging')
    folders={}
    for file in files:
        relative=Path(file['relative_path']);target=root/relative
        if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                or relative.parts[0]!='raw_data' or target.resolve()!=target
                or relative.name!=file['filename']):
            raise ValueError('Source partial path escaped approved layout')
        folders.setdefault(target.parent,set()).add(file['filename'])
    candidates=[]
    for folder,names in folders.items():
        for path in folder.iterdir():
            match=re.fullmatch(r'\.(.+)\.([A-Za-z0-9_-]{8})\.partial',path.name)
            if not match or match[1] not in names:
                continue  # Unknown files remain for the strict folder check.
            if match[1] in active_filenames:
                raise ValueError('Download temporary still belongs to an active file')
            if path.resolve()!=path or not path.is_file():
                raise ValueError('Unsafe source temporary')
            before=path.stat();sha=hashlib.sha256(path.read_bytes()).hexdigest();info=path.stat()
            if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)
                    !=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)):
                raise ValueError('Temporary changed; preserve active work')
            candidates.append((path,info,sha,match[1]))
    preserved=[]
    for path,info,sha,name in candidates:
        destination=quarantine/sha/path.name;destination.parent.mkdir(parents=True,exist_ok=True)
        if path.stat()!=info:
            raise ValueError('Temporary changed before preservation')
        os.link(path,destination)  # Exclusive; never overwrite prior evidence.
        current=path.stat()
        if ((current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)
                !=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
                or hashlib.sha256(destination.read_bytes()).hexdigest()!=sha):
            raise ValueError('Temporary preservation differs; source entry retained')
        path.unlink()
        preserved.append(dict(filename=name,temporary_name=path.name,
                              sha256=sha,size_bytes=info.st_size,
                              preserved_copy=str(destination)))
    return preserved
