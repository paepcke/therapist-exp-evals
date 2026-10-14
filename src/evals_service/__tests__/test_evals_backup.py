# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-13 17:26:44
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/__tests__/test_evals_backup.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-13 17:26:44
# **********************************************************

'''
Tests for the database snapshot.
'''

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals_service.eval_store import EvalStore
from evals_service.evals_backup import BackupError, EvalsBackup
from evals_service.evals_common import EvalsCommon


class BackupTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'evals'
        self.store = EvalStore(self.root / 'data' / 'evals.sqlite')
        self.jon = self.store.add_person('jon@x.org', ['evaluator'])
        self.store.submit(self.jon, 'session_review', f'{1:016x}',
                          {'student': {'anxiety': 7}, 'trainer': {}, 'comments': {}}, 'v1')
        self.store.save_draft(self.jon, 'session_review', f'{2:016x}', {'student': {'anxiety': 3}}, 'v1')
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'roster.json').write_text('{"cohorts": []}')
        self.now = datetime(2026, 11, 1, 3, 30, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backup(self, keep=3):
        return EvalsBackup(self.root, clock=lambda: self.now, keep=keep)

    def test_snapshot_is_complete_and_described(self):
        latest = self.backup().run()
        d = self.root / 'backup'
        snap = d / latest['file']
        self.assertEqual(snap.name, 'evals-20261101T033000Z.sqlite')
        self.assertEqual(latest['sha256'], hashlib.sha256(snap.read_bytes()).hexdigest())
        self.assertEqual((latest['people'], latest['ratings_submitted'], latest['ratings_draft']), (1, 1, 1))
        self.assertEqual(json.loads((d / 'LATEST.json').read_text()), latest)
        self.assertEqual((d / 'roster.json').read_text(), '{"cohorts": []}')
        self.assertIn('jon@x.org,session_review', (d / latest['csv']).read_text())
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name.startswith('.')), [])

    def test_snapshot_opens_and_holds_the_data(self):
        latest = self.backup().run()
        db = sqlite3.connect(f"file:{self.root / 'backup' / latest['file']}?immutable=1", uri=True)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM ratings WHERE state='submitted'").fetchone()[0], 1)
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        db.close()

    def test_old_snapshots_are_pruned_with_their_csv(self):
        for i in range(5):
            self.now += timedelta(days=1)
            self.backup(keep=3).run()
        d = self.root / 'backup'
        self.assertEqual(len(list(d.glob('evals-*.sqlite'))), 3)
        self.assertEqual(len(list(d.glob('ratings-*.csv'))), 3)
        newest = sorted(d.glob('evals-*.sqlite'))[-1].name
        self.assertEqual(json.loads((d / 'LATEST.json').read_text())['file'], newest)

    def test_live_database_is_not_changed_in_its_content(self):
        before = self.store.export_submitted()
        self.backup().run()
        self.assertEqual(self.store.export_submitted(), before)

    def test_restore_by_copying_the_snapshot(self):
        latest = self.backup().run()
        restored = self.tmp / 'restored'
        (restored / 'data').mkdir(parents=True)
        shutil.copyfile(self.root / 'backup' / latest['file'], restored / 'data' / 'evals.sqlite')
        store = EvalStore(restored / 'data' / 'evals.sqlite')
        try:
            self.assertEqual(store.person_by_email('jon@x.org')['id'], self.jon)
            self.assertEqual(len(store.export_submitted()), 1)
            self.assertEqual(store.get_rating(self.jon, 'session_review', f'{2:016x}')['state'], 'draft')
        finally:
            store.close()

    def test_separate_backup_directory(self):
        elsewhere = self.tmp / 'elsewhere'
        latest = EvalsBackup(self.root, clock=lambda: self.now, backup_dir=elsewhere).run()
        self.assertTrue((elsewhere / latest['file']).is_file())
        self.assertFalse((self.root / 'backup').exists())

    def test_missing_database(self):
        with self.assertRaises(BackupError):
            EvalsBackup(self.tmp / 'nowhere').run()

    def test_main_returns_codes(self):
        self.assertEqual(EvalsBackup.main(['--service-root', str(self.root)]), 0)
        self.assertEqual(EvalsBackup.main(['--service-root', str(self.tmp / 'nowhere')]), 1)


if __name__ == '__main__':
    unittest.main()
