import contextlib
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import clone
import cloning
import server
from firstrun_gen import make_identity_firstrun

class ProductionTests(unittest.TestCase):
    def test_unsupported_layout_rejected(self):
        with self.assertRaises(clone.CloneError):
            clone.validate_layout([{'num':1,'fstype':'ext4'},{'num':2,'fstype':'vfat'}])
        clone.validate_layout([{'num':1,'fstype':'vfat'},{'num':2,'fstype':'ext4'}])

    def test_hostnames_and_shell(self):
        for value in ('', '../pi', 'pi;reboot', '-pi', 'pi-', 'x'*64):
            with self.assertRaises(ValueError): make_identity_firstrun(value)
        script = make_identity_firstrun('factory-001')
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('hostnamectl', script)

    def test_reject_system_disk_before_start(self):
        with patch.object(server, 'list_devices', return_value=[{'device':'/dev/sdb'}]):
            with self.assertRaises(ValueError):
                cloning.start(server, {'operation':'write','name':'master','devices':['/dev/nvme0n1']})

    def test_identity_required_before_writes(self):
        with patch.object(clone,'check_card') as check:
            with self.assertRaises(clone.CloneError):clone.write_copy('/dev/sdb','master')
            with self.assertRaises(ValueError):clone.write_copy('/dev/sdb','master','pi;reboot')
            check.assert_not_called()

    def test_cleanup_preserves_software_and_removes_copied_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp,'root');boot=pathlib.Path(temp,'boot');home=pathlib.Path(temp,'home')
            for folder in (root,boot,home):folder.mkdir()
            for rel in ('etc/ssh/ssh_host_ed25519_key','etc/machine-id','var/lib/tailscale/tailscaled.state','etc/pi-provision/key','home/kniti/.post_install_done','opt/application/main.py'):
                path=root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('master')
            (home/'.post_install_done').write_text('done')
            (boot/'cmdline.txt').write_text('console=serial0 root=PARTUUID=abc-02 quiet')
            @contextlib.contextmanager
            def mount(part,readonly=False):
                yield str({'/dev/sdb1':boot,'/dev/sdb2':root,'/dev/sdb8':home}[part])
            with patch.object(clone,'mounted',mount),patch.object(clone,'partition_table',return_value={'partitions':[{'num':2,'start':100,'type':'83'},{'num':8,'start':200,'type':'83'}]}):
                clone.give_identity('/dev/sdb','factory-002')
            self.assertFalse((root/'var/lib/tailscale/tailscaled.state').exists())
            self.assertFalse((root/'etc/pi-provision').exists())
            self.assertFalse((home/'.post_install_done').exists())
            self.assertEqual((root/'opt/application/main.py').read_text(),'master')
            self.assertIn('systemd.run=',(boot/'cmdline.txt').read_text())
            self.assertIn('factory-002',(boot/'firstrun.sh').read_text())

if __name__=='__main__':unittest.main()
