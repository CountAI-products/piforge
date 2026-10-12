"""Background sparse archive creation for the PiForge desktop UI."""
import datetime
import os
from pathlib import Path
import threading
import time
import clone
import share_image

LOCK=threading.Lock()
STATE={'state':'idle','message':'Choose an image to create a shareable file.'}


def snapshot():
    with LOCK:
        result=dict(STATE)
        if result.get('started') and result['state'] in ('preparing','compressing'):
            result['seconds']=round(time.monotonic()-result['started'])
        result.pop('started',None)
        return result


def start(name):
    image,metadata=clone.image_paths(name)
    if not Path(image).is_file() or not Path(metadata).is_file():
        raise ValueError('Select a saved PiForge image')
    folder=Path(clone.CLONE_DIR,'shared');folder.mkdir(parents=True,exist_ok=True)
    with LOCK:
        if STATE['state'] in ('preparing','compressing'):
            raise ValueError('A shareable file is already being created')
        STATE.clear();STATE.update(state='preparing',message='Preparing block map…',seconds=0,started=time.monotonic())
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output=folder/f'{name}-{stamp}.tar.gz'
    def worker():
        started=time.monotonic()
        def update(phase,done=None,total=None):
            with LOCK:
                STATE.update(state=phase,seconds=round(time.monotonic()-started),message='Compressing image and metadata; this can take several minutes.' if phase=='compressing' else 'Preparing reusable block map…')
                if done is not None:STATE.update(mapped_bytes=done,total_bytes=total)
        try:
            share_image.export(image,str(output),metadata_path=metadata,progress=update)
            clone._chown_real(str(output));clone._chown_real(str(folder))
            with LOCK:STATE.update(state='done',message='Shareable file ready',path=str(output),bytes=output.stat().st_size,seconds=round(time.monotonic()-started))
        except Exception as error:
            with LOCK:STATE.update(state='error',message=str(error),seconds=round(time.monotonic()-started))
    threading.Thread(target=worker,daemon=True).start()
