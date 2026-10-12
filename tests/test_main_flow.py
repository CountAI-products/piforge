import http.server
import json
import threading
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch
import server

class MainFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_usual_flash_routes_master_to_clone_with_tailscale(self):
        payload = {'devices':['/dev/sdb'], 'config':{'image_url':'master:production', 'hostname':'mill', 'first_number':11,'inject_tailscale':True}}
        request = urllib.request.Request(self.url+'/api/flash', data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with patch('cloning.start') as start, patch('server.os.geteuid',return_value=0):
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)['skip_partition'])
            actual = start.call_args.args[1]
            self.assertEqual(actual['name'],'production')
            self.assertEqual(actual['first'],11)
            self.assertTrue(actual['tailscale'])

    def test_finished_card_cannot_be_repartitioned(self):
        with patch.dict(server.JOBS, {'/dev/sdb':{'master_image':True}}):
            with self.assertRaisesRegex(ValueError,'already contain partitions'):
                server.start_partition(['/dev/sdb'],{})

    def test_image_capture_is_on_existing_page(self):
        with urllib.request.urlopen(self.url+'/') as response:
            page=response.read().decode()
        self.assertIn('Make image from selected card',page)
        self.assertIn('Installed card image',page)
        self.assertNotIn('href="/clone.html"',page)
        with urllib.request.urlopen(self.url+'/clone.html') as response:
            self.assertEqual(response.url,self.url+'/')

if __name__=='__main__':unittest.main()
