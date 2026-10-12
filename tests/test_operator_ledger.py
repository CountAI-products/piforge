import csv
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import server

class OperatorLedgerTests(unittest.TestCase):
    def test_migration_attribution_and_total_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder,'ledger.csv')
            path.write_text('timestamp,device,hostname,token_prefix,key_id,status\nold,/dev/old,old,token,key,issued\n')
            with patch.object(server,'IMAGES_DIR',folder),patch.object(server,'TAILSCALE_LEDGER_PATH',str(path)),patch.dict(server.JOBS,{'/dev/test':{'installer':'Operator A','hostname':'pi-001','issued_at':'2026-10-12T10:00:00Z','time_source':'tailscale_http_date'}}):
                server.log_tailscale_ledger('/dev/test','','short','id','issued')
                server.update_install_duration('/dev/test',123.45)
                rows=server.read_tailscale_ledger()
                self.assertEqual(rows[0]['installer'],'Operator A')
                self.assertEqual(rows[0]['timestamp'],'2026-10-12T10:00:00Z')
                self.assertEqual(rows[0]['total_seconds'],'123.5')
                self.assertEqual(len(rows),2)
