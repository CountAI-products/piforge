import unittest
from unittest.mock import patch
import operator_auth

class ProxyTests(unittest.TestCase):
    def tearDown(self):operator_auth.SESSION=None
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
