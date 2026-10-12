"""Actual file copy telemetry through clone orchestration, no card writes."""
import contextlib
import io
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import blockmap
import clone
import cloning
import server

class ProgressFlowTests(unittest.TestCase):
    def test_real_copy_reports_progress_and_only_finishes_after_verify(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder,'source.img');source.write_bytes(b'X'*(10*1024*1024))
            target=Path(folder,'target.img');target.touch()
            mapping=blockmap.build(str(source))
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                print('STEP copy');print('COPY image')
                blockmap.copy(str(source),str(target),mapping,clone.map_progress('image'))
                print('STEP flush');print('PROGRESS image 100');print('STEP verify')
            self.assertEqual(source.read_bytes(),target.read_bytes())
            self.assertIn('PROGRESS image 99',output.getvalue())
            self.assertIn('METRICS ',output.getvalue())
            events=[];finished=threading.Event()
            class Child:
                stdout=io.StringIO(output.getvalue())
                pid=0
                def wait(self):return 0
            original=server.set_job
            def capture(device,**fields):
                events.append(fields.copy());original(device,**fields)
                if fields.get('state')=='done':finished.set()
            with patch.object(server,'list_devices',return_value=[{'device':'/dev/test-card'}]),patch.object(clone,'image_paths',return_value=(str(source),str(source))),patch.object(cloning.subprocess,'Popen',return_value=Child()),patch.object(server,'set_job',side_effect=capture):
                cloning.start(server,{'operation':'write','name':'test','devices':['/dev/test-card'],'tailscale':False})
                self.assertTrue(finished.wait(3),'Worker did not complete')
                # Ensure runner cleanup finishes before mocks leave scope.
                for _ in range(100):
                    if not server.FLASH_ACTIVE.is_set():break
                    threading.Event().wait(.01)
            percentages=[event['percent'] for event in events if 'percent' in event]
            self.assertIn(99,percentages)
            metrics=[event for event in events if 'mapped blocks' in event.get('message','')]
            self.assertTrue(metrics)
            self.assertGreaterEqual(metrics[-1]['elapsed_s'],0)
            self.assertIn('ETA',metrics[-1]['message'])
            verification=next(i for i,event in enumerate(events) if event.get('state')=='verifying')
            done=next(i for i,event in enumerate(events) if event.get('state')=='done')
            self.assertLess(verification,done)
