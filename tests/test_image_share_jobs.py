import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import clone
import image_share_jobs
import tarfile

class SharingJobTests(unittest.TestCase):
    def test_background_archive_with_matching_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);image=root/'master.img'
            with image.open('wb') as file:file.write(b'boot');file.seek(1024*1024);file.write(b'data')
            (root/'master.json').write_text(json.dumps({'name':'master'}))
            with patch.object(clone,'CLONE_DIR',folder):
                image_share_jobs.start('master')
                state={}
                for _ in range(100):
                    state=image_share_jobs.snapshot()
                    if state['state'] in ('done','error'):break
                    threading.Event().wait(.02)
                self.assertEqual(state['state'],'done',state)
                archive=Path(state['path']);self.assertTrue(archive.is_file())
                with tarfile.open(archive) as file:
                    self.assertEqual(set(file.getnames()),{'master.img','master.json'})
                    metadata=json.load(file.extractfile('master.json'))
                    self.assertIn('block_map',metadata)
    def test_duplicate_job_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(clone,'CLONE_DIR',folder):
            Path(folder,'master.img').touch();Path(folder,'master.json').write_text('{}')
            with patch.dict(image_share_jobs.STATE,{'state':'compressing'}):
                with self.assertRaises(ValueError):image_share_jobs.start('master')
