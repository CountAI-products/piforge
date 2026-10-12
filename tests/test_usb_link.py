from pathlib import Path
import tempfile
import unittest
from server import usb_link_info

class UsbLinkTests(unittest.TestCase):
    def test_reader_speed_not_root_hub_speed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);hub=root/'devices/usb1';reader=hub/'1-2';device=reader/'1-2:1.0/host0/disk'
            device.mkdir(parents=True)
            for node,speed in ((hub,'10000'),(reader,'480')):
                (node/'idVendor').write_text('1234');(node/'speed').write_text(speed)
            link=root/'class/block/sda';link.mkdir(parents=True);(link/'device').symlink_to(device)
            self.assertEqual(usb_link_info('/dev/sda',folder)['usb_link'],'USB 2.0 · 480 Mbps')
            (reader/'speed').write_text('5000')
            self.assertEqual(usb_link_info('/dev/sda',folder)['usb_link'],'USB 3.x · 5000 Mbps')
            (reader/'speed').write_text('unknown')
            self.assertIsNone(usb_link_info('/dev/sda',folder)['usb_speed_mbps'])
    def test_missing_device(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(usb_link_info('/dev/missing',folder)['usb_speed_mbps'])
