"""
Ops scripts (bash): daily DB backup and deploy.
Run: python -m unittest test_ops
"""

import gzip
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent / 'ops'


def run(script, env, args=()):
    return subprocess.run(['bash', str(OPS / script), *args], env={**os.environ, **env},
                          capture_output=True, text=True, timeout=60)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'daily'
        self.env = {'BACKUP_DIR': str(self.dir), 'KEEP': '3', 'DB_NAME': 'luxpower',
                    'PG_DUMP_CMD': 'echo fake-dump-of'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_gzipped_private_dump(self):
        result = run('backup-db.sh', self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        dumps = list(self.dir.glob('luxpower-*.sql.gz'))
        self.assertEqual(len(dumps), 1)
        self.assertEqual(gzip.decompress(dumps[0].read_bytes()).decode().strip(),
                         'fake-dump-of luxpower')
        self.assertEqual(stat.S_IMODE(dumps[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertIn('backup:', result.stdout)

    def test_keeps_only_newest(self):
        self.dir.mkdir(parents=True)
        now = time.time()
        for i in range(5):
            old = self.dir / f'luxpower-2026010{i}-030000.sql.gz'
            old.write_bytes(b'old')
            os.utime(old, (now - (10 - i) * 86400, now - (10 - i) * 86400))
        self.assertEqual(run('backup-db.sh', self.env).returncode, 0)
        names = sorted(p.name for p in self.dir.glob('luxpower-*.sql.gz'))
        self.assertEqual(len(names), 3)
        self.assertIn('luxpower-20260104-030000.sql.gz', names)
        self.assertIn('luxpower-20260103-030000.sql.gz', names)

    def test_failed_dump_leaves_no_file_and_fails(self):
        env = {**self.env, 'PG_DUMP_CMD': 'false'}
        result = run('backup-db.sh', env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.dir.glob('luxpower-*')), [])


SHA256 = 'sha256sum' if shutil.which('sha256sum') else 'shasum -a 256'

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
if [ "$1" = "is-active" ]; then
    for u in "$@"; do [ "$u" = "is-active" ] || echo "${FAKE_STATE:-active}"; done
    [ "${FAKE_STATE:-active}" = "active" ]
fi
"""

FAKE_JOURNALCTL = """#!/bin/sh
cat "$FAKE_JOURNAL"
"""


class DeployTest(unittest.TestCase):
    COMMIT = 'abc1234'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # source tarball shaped like a GitHub archive
        src = root / 'build' / 'luxpower-grid-monitor-abc1234' / 'vps'
        src.mkdir(parents=True)
        (src / 'bot.py').write_text("print('new bot')\n")
        (src / 'test_bot.py').write_text("assert True\n")
        (src / 'requirements.txt').write_text("requests\n")
        self.src = src
        self.tgz = root / 'src.tgz'
        self._pack()
        # current app
        self.app = root / 'app'
        self.app.mkdir()
        (self.app / 'bot.py').write_text("print('old bot')\n")
        (self.app / 'requirements.txt').write_text("requests\n")
        # stub tools
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        for name, body in (('systemctl', FAKE_SYSTEMCTL), ('journalctl', FAKE_JOURNALCTL)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.log = root / 'systemctl.log'
        self.journal = root / 'journal.txt'
        self.journal.write_text("[INFO] Public bot started\n")
        self.backups = root / 'backups'
        self.env = {
            'PATH': f"{bin_dir}:{os.environ['PATH']}",
            'SOURCE_TGZ': str(self.tgz), 'APP_DIR': str(self.app),
            'BACKUP_ROOT': str(self.backups), 'UNITS': 'bot-a bot-b',
            'INSTALL_OPTS': '', 'SHA256': SHA256, 'SETTLE_SECONDS': '0',
            'PYTHON': sys.executable, 'FAKE_LOG': str(self.log),
            'FAKE_JOURNAL': str(self.journal), 'TMPDIR': self.tmp.name,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _pack(self):
        subprocess.run(['tar', '-czf', str(self.tgz), '-C', str(self.src.parent.parent),
                        self.src.parent.name], check=True)

    def manifest(self, files=('bot.py', 'requirements.txt'), tamper=False):
        lines = []
        for name in files:
            digest = hashlib.sha256((self.src / name).read_bytes()).hexdigest()
            if tamper:
                digest = '0' * 64
            lines.append(f"{digest}  {name}\n")
        path = Path(self.tmp.name) / 'manifest.sha256'
        path.write_text(''.join(lines))
        return str(path)

    def deploy(self, *args):
        return run('deploy.sh', self.env, args or (self.COMMIT, self.manifest()))

    def systemctl_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_happy_path(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.app / 'bot.py').read_text(), "print('new bot')\n")
        self.assertFalse((self.app / 'test_bot.py').exists())
        self.assertIn('restart bot-a bot-b', self.systemctl_calls())
        (backup,) = self.backups.glob('code-*')
        self.assertEqual((backup / 'bot.py').read_text(), "print('old bot')\n")
        self.assertIn('tracebacks=0 errors=0 token_urls=0', result.stdout)

    def test_checksum_mismatch_deploys_nothing(self):
        result = self.deploy(self.COMMIT, self.manifest(tamper=True))
        self.assertEqual(result.returncode, 3)
        self.assertEqual((self.app / 'bot.py').read_text(), "print('old bot')\n")
        self.assertEqual(self.systemctl_calls(), [])

    def test_manifest_must_cover_every_deployed_file(self):
        result = self.deploy(self.COMMIT, self.manifest(files=('bot.py',)))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.systemctl_calls(), [])

    def test_syntax_error_deploys_nothing(self):
        (self.src / 'bot.py').write_text("def broken(:\n")
        self._pack()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / 'bot.py').read_text(), "print('old bot')\n")
        self.assertEqual(self.systemctl_calls(), [])

    def test_errors_in_logs_fail_with_rollback_hint(self):
        self.journal.write_text("Traceback (most recent call last):\n[ERROR] boom\n")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn('rollback', result.stderr)

    def test_inactive_unit_fails_with_rollback_hint(self):
        self.env['FAKE_STATE'] = 'failed'
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn('rollback', result.stderr)

    def test_bad_commit_argument(self):
        result = run('deploy.sh', self.env, ('main; rm -rf /',))
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
