# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-13 17:26:44
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/backup_pull/__tests__/test_backup_puller.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-13 17:26:44
# **********************************************************

'''
Tests for the laptop-side backup puller. rsync, mail and notifications are replaced
by fakes; the snapshots come from the real EvalsBackup.
'''

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backup_pull.backup_puller import BackupPuller, PullConfig, SnapshotVerifier, StatusFile, VerifyError
from evals_service.eval_store import EvalStore
from evals_service.evals_backup import EvalsBackup


class FakeRunner:
    '''
    "Pulls" by copying. remote -> local source path, or an exception to raise.
    '''

    def __init__(self):
        self.sources = {}
        self.failing = {}
        self.calls = []

    def pull(self, remote, dest, ssh_key=None):
        self.calls.append((remote, ssh_key))
        if remote in self.failing:
            raise RuntimeError(self.failing[remote])
        src = Path(self.sources[remote])
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest / src.name)


class FakeNotifier:

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class PullerTests(unittest.TestCase):

    VULTR = 'evpull@vultr:/'
    QUATRO_SECRET = 'thtrainer@quatro:.ssh/eval_pseudonym_secret.txt'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # A server with a database and a snapshot:
        self.root = self.tmp / 'server' / 'evals'
        store = EvalStore(self.root / 'data' / 'evals.sqlite')
        store.add_person('jon@x.org', ['evaluator'])
        store.close()
        self.now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.snapshot_time = self.now
        self.make_snapshot()
        secret = self.tmp / 'server' / 'eval_pseudonym_secret.txt'
        secret.write_text('s3cret\n')

        self.runner, self.notifier = FakeRunner(), FakeNotifier()
        self.runner.sources = {self.VULTR: self.root / 'backup', self.QUATRO_SECRET: secret}
        self.config = PullConfig({
            'dest_root': str(self.tmp / 'laptop'),
            'sources': [
                {'name': 'vultr-evals', 'remotes': [self.VULTR], 'verify': 'evals_snapshot', 'ssh_key': '~/k'},
                {'name': 'quatro-secrets', 'remotes': [self.QUATRO_SECRET], 'hint': 'Start the Stanford VPN.'}]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_snapshot(self):
        EvalsBackup(self.root, clock=lambda: self.snapshot_time).run()

    def puller(self):
        return BackupPuller(self.config, self.runner, self.notifier, clock=lambda: self.now)

    def advance(self, **kw):
        self.now += timedelta(**kw)

    # ------------------------------------------------------------

    def test_first_run_pulls_and_verifies_everything(self):
        pulled = self.puller().run()
        self.assertEqual(pulled, ['vultr-evals', 'quatro-secrets'])
        dest = self.config.dest_root
        self.assertTrue((dest / 'vultr-evals' / 'LATEST.json').is_file())
        self.assertEqual((dest / 'quatro-secrets' / 'eval_pseudonym_secret.txt').read_text(), 's3cret\n')
        self.assertEqual(oct((dest / 'quatro-secrets' / 'eval_pseudonym_secret.txt').stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(dest.stat().st_mode & 0o777), '0o700')
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.runner.calls[0], (self.VULTR, '~/k'))

    def test_sources_are_not_pulled_again_before_the_retry_interval(self):
        self.puller().run()
        self.runner.calls.clear()
        self.advance(hours=3)
        self.assertEqual(self.puller().run(), [])
        self.assertEqual(self.runner.calls, [])
        self.advance(hours=10)
        self.assertEqual(len(self.puller().run()), 2)
        self.assertEqual(len(self.puller().run(force=True)), 2)

    def test_short_outage_does_not_alert(self):
        self.puller().run()
        self.runner.failing[self.QUATRO_SECRET] = 'ssh: connect timed out'
        self.advance(hours=30)
        self.snapshot_time = self.now
        self.make_snapshot()
        self.puller().run()
        self.assertEqual(self.notifier.sent, [])
        self.assertIn('timed out', self.puller().status.of('quatro-secrets')['last_error'])

    def test_long_outage_alerts_once_a_day_with_hint(self):
        self.puller().run()
        self.runner.failing[self.QUATRO_SECRET] = 'ssh: connect timed out'
        for _ in range(3):                     # three days, hourly runs would repeat; one per run here
            self.advance(hours=24)
            self.snapshot_time = self.now
            self.make_snapshot()
            self.puller().run()
        subjects = [s for s, _ in self.notifier.sent]
        self.assertEqual(len(subjects), 1)
        self.assertIn('quatro-secrets not backed up for', subjects[0])
        self.assertIn('Start the Stanford VPN.', self.notifier.sent[0][1])
        self.assertIn('connect timed out', self.notifier.sent[0][1])
        self.advance(hours=25)
        self.snapshot_time = self.now
        self.make_snapshot()
        self.puller().run()
        self.assertEqual(len(self.notifier.sent), 2)         # repeats after alert_repeat_hours

    def test_recovery_stops_alerts(self):
        self.puller().run()
        self.runner.failing[self.QUATRO_SECRET] = 'down'
        self.advance(hours=60)
        self.snapshot_time = self.now
        self.make_snapshot()
        self.puller().run()
        self.assertEqual(len(self.notifier.sent), 1)
        del self.runner.failing[self.QUATRO_SECRET]
        self.advance(hours=25)
        self.snapshot_time = self.now
        self.make_snapshot()
        self.puller().run()
        self.assertEqual(len(self.notifier.sent), 1)

    def test_stale_server_snapshots_alert_even_though_the_pull_works(self):
        self.puller().run()
        self.advance(hours=60)                 # the server made no new snapshot
        self.puller().run()
        subjects = [s for s, _ in self.notifier.sent]
        self.assertEqual(len(subjects), 1)
        self.assertIn('vultr-evals', subjects[0])
        self.assertIn('seems to have stopped', self.notifier.sent[0][1])

    def test_never_backed_up_alerts(self):
        self.runner.failing[self.VULTR] = 'refused'
        self.puller().run()
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn('never been backed up', self.notifier.sent[0][0])

    def test_damaged_snapshot_is_not_counted_as_success(self):
        snap = next((self.root / 'backup').glob('evals-*.sqlite'))
        snap.write_bytes(snap.read_bytes()[:-100] + b'x' * 100)
        pulled = self.puller().run()
        self.assertEqual(pulled, ['quatro-secrets'])
        self.assertIn('checksum', self.puller().status.of('vultr-evals')['last_error'])

    def test_verifier_rejects_missing_latest_and_missing_file(self):
        with self.assertRaises(VerifyError):
            SnapshotVerifier.verify(self.tmp / 'empty')
        d = self.tmp / 'copy'
        shutil.copytree(self.root / 'backup', d)
        next(d.glob('evals-*.sqlite')).unlink()
        with self.assertRaisesRegex(VerifyError, 'not transferred'):
            SnapshotVerifier.verify(d)

    def test_report_and_exit_codes(self):
        p = self.puller()
        p.run()
        self.assertIn('vultr-evals', p.report())
        self.assertIn('newest data 0 hours old', p.report())

    def test_config_errors(self):
        with self.assertRaises(ValueError):
            PullConfig({})
        with self.assertRaises(ValueError):
            PullConfig({'sources': [{'name': 'x'}]})
        with self.assertRaises(ValueError):
            PullConfig({'sources': [{'name': 'x', 'remotes': ['a'], 'verify': 'magic'}]})

    def test_status_file_survives_garbage(self):
        p = self.tmp / 's.json'
        p.write_text('not json')
        self.assertEqual(StatusFile(p).sources, {})


if __name__ == '__main__':
    unittest.main()
