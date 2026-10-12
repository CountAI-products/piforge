import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import localimages
import clone

class LocalImageTests(unittest.TestCase):
    def test_picker_lists_images_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'master.img').write_bytes(b'card')
            (root/'master.json').write_text('{}')
            (root/'private.txt').write_text('not an image')
            (root/'cards').mkdir()
            result=localimages.browse(directory)
            self.assertEqual([entry['name'] for entry in result['entries']],['cards','master.img'])
            self.assertTrue(result['entries'][1]['metadata'])

    def test_matching_metadata_required(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory,'master.img');image.write_bytes(b'card')
            with self.assertRaisesRegex(ValueError,'matching PiForge'):
                localimages.register(str(image))

    def test_register_uses_source_without_copy_and_checks_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);image=root/'master.img';image.write_bytes(b'card')
            parts=[{'num':1,'start':0,'size':1,'type':'c','fstype':'vfat'}, {'num':2,'start':1,'size':1,'type':'83','fstype':'ext4'}]
            meta={'partitions':parts,'image_bytes':4,'min_card_bytes':4}
            image.with_suffix('.json').write_text(json.dumps(meta))
            with patch.object(clone,'CLONE_DIR',str(root/'registry')),patch.object(clone,'partition_table',return_value={'partitions':parts}),patch.object(clone,'fs_type_at',side_effect=['vfat','ext4']):
                result=localimages.register(str(image))
                registered=Path(clone.image_paths(result['name'])[0])
                self.assertTrue(registered.is_symlink())
                self.assertEqual(registered.resolve(),image)
                self.assertEqual(image.read_bytes(),b'card')
            meta['image_bytes']=5
            image.with_suffix('.json').write_text(json.dumps(meta))
            with patch.object(clone,'partition_table',return_value={'partitions':parts}):
                with self.assertRaisesRegex(ValueError,'size'):
                    localimages.register(str(image))

if __name__=='__main__':unittest.main()
