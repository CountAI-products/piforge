"""Browse station folders and register a PiForge image without uploading it."""
import hashlib
import json
import os
from pathlib import Path
import clone
import server


def browse(folder=None):
    folder = Path(folder or clone.CLONE_DIR).expanduser().resolve()
    if not folder.exists() and str(folder) == clone.CLONE_DIR:
        folder = Path(server.real_home())
    if not folder.is_dir():
        raise ValueError('Select an existing folder')
    entries = []
    for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if entry.name.startswith('.'):
            continue
        if entry.is_dir() or (entry.is_file() and entry.suffix == '.img'):
            entries.append({'name':entry.name, 'path':str(entry), 'directory':entry.is_dir(),
                            'metadata': entry.with_suffix('.json').is_file() if entry.is_file() else False})
    return {'folder':str(folder), 'parent':str(folder.parent), 'entries':entries}


def register(path):
    image = Path(path).expanduser().resolve(strict=True)
    if not image.is_file() or image.suffix != '.img':
        raise ValueError('Select a PiForge .img file')
    sidecar = image.with_suffix('.json')
    if not sidecar.is_file():
        raise ValueError('Keep the matching PiForge .json metadata beside the .img file')
    with sidecar.open() as file:
        meta = json.load(file)
    table = clone.partition_table(str(image))
    actual = clone.data_partitions(table)
    clone.validate_layout(meta['partitions'])
    expected = meta['partitions']
    keys = ('num','start','size','type')
    if [{k:p[k] for k in keys} for p in actual] != [{k:p[k] for k in keys} for p in expected]:
        raise ValueError('Image partition table does not match its PiForge metadata')
    if meta['image_bytes'] != image.stat().st_size or meta['min_card_bytes'] != meta['image_bytes']:
        raise ValueError('Image size does not match its metadata')
    for part in expected:
        if clone.fs_type_at(str(image),part['start']*clone.SECTOR) != part['fstype']:
            raise ValueError('Image filesystem does not match its metadata')
    name = 'local-' + hashlib.sha256(str(image).encode()).hexdigest()[:16]
    if image.parent == Path(clone.CLONE_DIR).resolve():
        return {'name':image.stem, 'path':str(image)}
    dest, metadata = clone.image_paths(name)
    os.makedirs(clone.CLONE_DIR,exist_ok=True)
    if os.path.lexists(dest):
        if not os.path.islink(dest) or Path(dest).resolve() != image:
            raise ValueError('A different image already uses this registration')
    else:
        os.symlink(str(image),dest)
    meta['name'] = name
    meta['display_name'] = image.name
    temporary = metadata + '.part'
    with open(temporary,'w') as file:
        json.dump(meta,file,indent=2)
    os.replace(temporary,metadata)
    return {'name':name,'path':str(image)}
