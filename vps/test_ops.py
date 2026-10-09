"""
Ops scripts (bash): daily DB backup and deploy.
Run: python -m unittest test_ops
"""

import gzip
import os
import stat
import subprocess
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


if __name__ == '__main__':
    unittest.main()
