"""Quota reads must never leave processes behind (they once accumulated by the hundreds)."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil

import process_jail
import quota_sources

# A launcher that starts a long-lived grandchild and exits at once: the
# cmd.exe -> node -> codex shape after cmd.exe is gone. The grandchild's pid is
# printed so the test can watch it.
ORPHANING_LAUNCHER = '''import subprocess, sys, time
if sys.argv[1] == "launch":
    child = subprocess.Popen([sys.executable, __file__, "server"], stdout=sys.stdout)
    print(child.pid, flush=True)
else:
    time.sleep(120)
'''


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


@unittest.skipUnless(sys.platform == "win32", "job objects are Windows only")
class ContainmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.script = Path(self.folder.name) / "launcher.py"
        self.script.write_text(ORPHANING_LAUNCHER, encoding="utf-8")
        self.spare = []

    def tearDown(self):
        for pid in self.spare:            # never leave a test process running, even on failure
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        self.folder.cleanup()

    def test_grandchild_of_an_exited_launcher_ends_with_the_job(self):
        process = process_jail.popen([sys.executable, str(self.script), "launch"], stdout=subprocess.PIPE, text=True)
        grandchild = int(process.stdout.readline())
        self.spare.append(grandchild)
        process.wait(timeout=10)
        self.assertTrue(alive(grandchild))           # orphaned: the old psutil snapshot could not see it
        self.assertEqual(psutil.Process(grandchild).ppid(), process.pid)
        process_jail.stop(process)
        psutil.wait_procs([psutil.Process(grandchild)], timeout=5) if alive(grandchild) else None
        self.assertFalse(alive(grandchild))
        process.stdout.close()

    def test_run_ends_leftovers_and_a_timeout_ends_the_whole_tree(self):
        result = process_jail.run([sys.executable, str(self.script), "launch"], capture_output=True, text=True, timeout=20)
        grandchild = int(result.stdout.split()[0])
        self.spare.append(grandchild)
        time.sleep(0.3)
        self.assertFalse(alive(grandchild))
        hang = Path(self.folder.name) / "hang.py"
        hang.write_text("import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                        "time.sleep(120)\n", encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            process_jail.run([sys.executable, str(hang)], capture_output=True, timeout=1.5)
        self.assertLess(time.monotonic() - started, 10)
        leftovers = [p for p in psutil.process_iter(["cmdline"]) if str(hang) in " ".join(p.info["cmdline"] or [])]
        self.assertEqual(leftovers, [])

    def test_codex_rpc_close_reaches_processes_whose_launcher_exited(self):
        server = Path(self.folder.name) / "rpc.py"
        server.write_text('''import json, os, subprocess, sys
if len(sys.argv) > 1:
    subprocess.Popen([sys.executable, "-u", __file__], stdin=sys.stdin, stdout=sys.stdout)   # launcher exits now
else:
    for line in sys.stdin:
        request = json.loads(line)
        print(json.dumps({"id": request["id"], "result": {"pid": os.getpid()}}), flush=True)
''', encoding="utf-8")
        rpc = quota_sources._JsonRpcProcess([sys.executable, "-u", str(server), "launcher"])
        try:
            rpc.process.wait(timeout=10)                 # the launcher is gone ...
            leaf = rpc.request("ping", None, timeout=10)["pid"]
            self.spare.append(leaf)
            self.assertTrue(alive(leaf))                 # ... while its server keeps answering
        finally:
            rpc.close()
        time.sleep(0.3)
        self.assertFalse(alive(leaf))


if __name__ == "__main__":
    unittest.main()
