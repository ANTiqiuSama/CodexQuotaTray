import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_status
import quota_sources
import tps_sources


class ClaudeStatusTests(unittest.TestCase):
    def test_newest_native_report_skips_corrupt_and_expired_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.json').write_text(json.dumps({'updated_at': 1000, 'rate_limits': {
                'five_hour': {'used_percentage': 20, 'resets_at': 2000},
                'seven_day': {'used_percentage': 40, 'resets_at': 9000}}}))
            (root / 'bad.json').write_text('{broken')
            result = claude_status.latest(2100, root)
            self.assertEqual(set(result['rate_limits']), {'seven_day'})
            self.assertIsNone(claude_status.latest(100000, root))

    def test_native_quota_avoids_cli_and_uses_real_update_time(self):
        now = time.time()
        report = {'updated_at': now, 'rate_limits': {
            'five_hour': {'used_percentage': 23, 'resets_at': int(now + 1000)},
            'seven_day': {'used_percentage': 8, 'resets_at': int(now + 10000)}}}
        with patch.object(claude_status, 'latest', return_value=report), \
                patch.object(quota_sources.cli_quota, 'read_usage') as cli:
            value = quota_sources.get_claude_quota()
        cli.assert_not_called()
        self.assertEqual((value.remaining_percent, value.secondary_remaining_percent), (77, 92))
        self.assertEqual(value.updated_at, now)

    def test_installed_bridge_waits_for_native_data_without_starting_workers(self):
        with patch.object(claude_status, 'latest', return_value=None), \
                patch.object(claude_status, 'bridge_installed', return_value=True), \
                patch.object(quota_sources.cli_quota, 'read_usage') as cli:
            value = quota_sources.get_claude_quota()
        cli.assert_not_called()
        self.assertIn('等待 Claude Code', value.error)

    @unittest.skipUnless(os.name == 'nt', 'Windows status-line receiver')
    def test_receiver_saves_only_numeric_quota_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver = Path(tmp) / 'receiver.ps1'
            receiver.write_bytes(Path(claude_status.__file__).with_name('claude_status.ps1').read_bytes())
            payload = {'session_id': 'test-session', 'secret': 'NEVER-SAVE',
                       'rate_limits': {'five_hour': {'used_percentage': 15, 'resets_at': 1900000000}}}
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(receiver)],
                                    input=json.dumps(payload), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = (Path(tmp) / 'claude-status/test-session.json').read_text()
            self.assertNotIn('NEVER-SAVE', output)
            self.assertEqual(json.loads(output)['rate_limits']['five_hour']['used_percentage'], 15)

    def test_processes_distinguish_desktop_from_code(self):
        rows = [SimpleNamespace(info={'name':'claude.exe', 'exe': path}) for path in (
            r'C:\node_modules\@anthropic-ai\claude-code\bin\claude.exe',
            r'D:\WindowsApps\Claude_1\app\claude.exe', r'D:\WindowsApps\Claude_1\app\claude.exe')]
        with patch.object(tps_sources.psutil, 'process_iter', return_value=rows):
            result = {r['name']:r['processes'] for r in tps_sources.running_software()}
        self.assertEqual(result, {'Claude Code':1, 'Claude 桌面':2})


if __name__ == '__main__':
    unittest.main()
