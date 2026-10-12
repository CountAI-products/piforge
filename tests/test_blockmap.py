import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import blockmap
from share_image import export

class BlockMapTests(unittest.TestCase):
    def image(self,directory):
        path=Path(directory,'master.img')
        with path.open('wb') as file:
            file.write(b'BOOT'*1024)
            file.seek(16*1024*1024)
            file.write(b'ROOT'*1024)
            file.truncate(32*1024*1024)
        path.with_suffix('.json').write_text(json.dumps({'name':'master'}))
        return path

    def test_sparse_copy_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            image=self.image(directory)
            mapping=blockmap.ensure(str(image),str(image.with_suffix('.json')))
            self.assertLess(mapping['mapped_bytes'],image.stat().st_size)
            target=Path(directory,'target.img')
            with target.open('wb') as file:file.truncate(image.stat().st_size)
            with patch.object(blockmap,'build',side_effect=AssertionError('rescanned')):
                reused=blockmap.ensure(str(image),str(image.with_suffix('.json')))
                blockmap.copy(str(image),str(target),reused)
            self.assertEqual(image.read_bytes(),target.read_bytes())

    def test_corrupt_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image=self.image(directory);mapping=blockmap.build(str(image));target=Path(directory,'target');target.touch()
            with image.open('r+b') as file:file.write(b'BAD!')
            with self.assertRaisesRegex(ValueError,'checksum'):
                blockmap.copy(str(image),str(target),mapping)

    def test_invalid_ranges_rejected_before_target_open(self):
        with self.assertRaises(ValueError):
            blockmap.validate({'version':1,'image_bytes':100,'mapped_bytes':2,'ranges':[[99,2,'a'*64]]},100)

    def test_shared_archive_retains_sparse_image_and_map(self):
        with tempfile.TemporaryDirectory() as directory:
            image=self.image(directory);archive=Path(directory,'shared.tar.gz')
            export(image,archive)
            self.assertLess(archive.stat().st_size,1024*1024)
            folder=Path(directory,'receiver');folder.mkdir()
            subprocess.run(['tar','-xzf',str(archive),'-C',str(folder)],check=True)
            restored=folder/'master.img'
            self.assertEqual(image.read_bytes(),restored.read_bytes())
            self.assertLess(restored.stat().st_blocks*512,restored.stat().st_size)
            mapping=json.loads((folder/'master.json').read_text())['block_map']
            blockmap.validate(mapping,restored.stat().st_size)
            with patch.object(blockmap,'build',side_effect=AssertionError('rescanned after sharing')):
                blockmap.ensure(str(restored),str(folder/'master.json'))

if __name__=='__main__':unittest.main()

class Ext4DirtyTargetTests(unittest.TestCase):
    def test_zero_allocated_blocks_replace_old_target_data(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory,'filesystem.img')
            with image.open('wb') as file:file.truncate(16*1024*1024)
            subprocess.run(['mkfs.ext4','-q','-F',str(image)],check=True)
            partition={'num':2,'start':0,'size':image.stat().st_size//512,'fstype':'ext4'}
            mapping=blockmap.build(str(image),partitions=[partition])
            target=Path(directory,'dirty.img')
            target.write_bytes(b'\xa5'*image.stat().st_size)
            blockmap.copy(str(image),str(target),mapping)
            result=subprocess.run(['e2fsck','-fn',str(target)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
