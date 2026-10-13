import unittest
from unittest.mock import patch
import operator_auth

class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.store=patch.object(operator_auth,"STORE",None);self.store.start()
    def tearDown(self):operator_auth.SESSION=None;self.store.stop()
    def test_credentials_never_stored_in_station_session(self):
        config={'mint_endpoint':'https://ledger.example/mint'}
        result={'token':'temporary-session','user':{'id':1,'username':'alice'}}
        with patch.object(operator_auth,'request',return_value=result):
            operator_auth.login(config,'alice','secure-test-password',register=True)
        self.assertNotIn('password',operator_auth.SESSION)
        self.assertEqual(operator_auth.current(config,verify=False)['username'],'alice')
        self.assertIsNone(operator_auth.current({'mint_endpoint':'https://different.example/mint'},verify=False))
    def test_secure_transport_required(self):
        with self.assertRaises(ValueError):operator_auth.base_url({'mint_endpoint':'http://public.example/mint'})
        self.assertEqual(operator_auth.base_url({'mint_endpoint':'http://100.121.194.27/mint'}),'http://100.121.194.27')


class PersistentSessionTests(unittest.TestCase):
    def test_restart_restores_token_without_password_and_logout_removes_it(self):
        import tempfile,json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder,patch.object(operator_auth,"STORE",None):
            path=Path(folder,'session.json');operator_auth.configure_store(path)
            config={'mint_endpoint':'https://ledger.example/mint'}
            with patch.object(operator_auth,'request',return_value={'token':'session-secret','user':{'id':1,'username':'alice'}}):
                operator_auth.login(config,'alice','password-not-to-save')
            self.assertNotIn('password-not-to-save',path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            operator_auth.SESSION=None;operator_auth.configure_store(path)
            self.assertEqual(operator_auth.token(config),'session-secret')
            with patch.object(operator_auth,'request',return_value={'ok':True}):operator_auth.logout(config)
            self.assertFalse(path.exists())
