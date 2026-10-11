# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 09:53:04
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_dashboard/__tests__/test_dashboard.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:53:04
# **********************************************************

'''
Tests for the dashboard service. The server tests need Tornado and are
skipped where it is not installed. All logs are synthetic.
'''

import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from log_analysis.__tests__.test_log_analysis import LogAnalysisTestBase
from log_dashboard.dashboard_config import ConfigError, DashboardConfig
from log_dashboard.dashboard_data import DashboardData, IdentifyingDataError, ParticipantsUnavailable
from log_dashboard.password_file import PasswordFile

try:
    import tornado.testing
    from log_dashboard.dashboard_server import DashboardServer, SecurityMixin
except ImportError:
    tornado = None


class PasswordFileTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'auth.txt'

    def test_round_trip_and_wrong_answers(self):
        PasswordFile.create('maria', 'a long enough password').save(self.path)
        entry = PasswordFile.load(self.path)
        self.assertTrue(entry.verify('maria', 'a long enough password'))
        self.assertFalse(entry.verify('maria', 'a long enough passwore'))
        self.assertFalse(entry.verify('mario', 'a long enough password'))
        self.assertFalse(entry.verify('', ''))

    def test_file_is_private_and_holds_no_password(self):
        PasswordFile.create('maria', 'a long enough password').save(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn('a long enough password', self.path.read_text())
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['auth.txt'])

    def test_same_password_gets_different_salt(self):
        a = PasswordFile.create('u', 'a long enough password')
        b = PasswordFile.create('u', 'a long enough password')
        self.assertNotEqual(a._digest, b._digest)

    def test_bad_input_is_refused(self):
        for user, password in (('', 'a long enough password'), ('a$b', 'a long enough password'),
                               ('a b', 'a long enough password'), ('maria', 'short')):
            with self.assertRaises(ValueError):
                PasswordFile.create(user, password)

    def test_loading_garbage(self):
        for text in ('', 'nodollars', 'a$b', 'a$zz$zz', 'a$b$c$d'):
            self.path.write_text(text)
            with self.assertRaises(ValueError):
                PasswordFile.load(self.path)

    def test_command_line(self):
        with mock.patch('getpass.getpass', side_effect=['a long enough password'] * 2):
            self.assertEqual(PasswordFile.main(['--file', str(self.path), '--user', 'maria']), 0)
        self.assertTrue(PasswordFile.load(self.path).verify('maria', 'a long enough password'))
        self.path.unlink()
        with mock.patch('getpass.getpass', side_effect=['a long enough password', 'another one entirely']):
            self.assertEqual(PasswordFile.main(['--file', str(self.path), '--user', 'maria']), 1)
        self.assertFalse(self.path.exists())


class ConfigTests(LogAnalysisTestBase):

    def test_defaults_and_participants_path(self):
        cfg = DashboardConfig(self.raw, no_auth=True)
        self.assertEqual((cfg.bind, cfg.port, cfg.trial_case_count), ('127.0.0.1', 8081, 4))
        self.assertEqual(cfg.participants_path, self.raw / 'participants.json')

    def test_missing_log_dir(self):
        with self.assertRaises(ConfigError):
            DashboardConfig(self.dir / 'nope', no_auth=True)

    def test_no_auth_only_on_loopback(self):
        with self.assertRaises(ConfigError):
            DashboardConfig(self.raw, bind='0.0.0.0', no_auth=True)

    def test_missing_password_file_says_how_to_make_it(self):
        with self.assertRaises(ConfigError) as ctx:
            DashboardConfig(self.raw, auth_file=self.dir / 'absent')
        self.assertIn('password_file', str(ctx.exception))

    def test_cli_beats_environment(self):
        env = {'THDASH_LOG_DIR': str(self.raw), 'THDASH_PORT': '9000', 'THDASH_NO_AUTH': '1'}
        self.assertEqual(DashboardConfig.from_cli([], env).port, 9000)
        self.assertEqual(DashboardConfig.from_cli(['--port', '9100'], env).port, 9100)


class DataTests(LogAnalysisTestBase):

    def make(self, log_dir, **kwargs):
        return DashboardData(DashboardConfig(log_dir, no_auth=True, **kwargs))

    def test_document_of_deidentified_logs(self):
        data = self.make(self.write_deidentified_logs())
        doc = data.document(False)
        json.dumps(doc, allow_nan=False)                      # strictly valid JSON
        self.assertEqual(doc['kpis']['cases'], 2)
        self.assertEqual(doc['kpis']['student_turns'], 2)
        self.assertEqual(doc['kpis']['participants_started'], 2)
        self.assertEqual(doc['packs'], ['P1', '(none)'])
        self.assertEqual({p['id'] for p in doc['progress']}, {'aaaaaaaa', 'bbbbbbbb'})
        self.assertEqual(doc['origin'], [{'tz': 'Europe/Berlin', 'area': 'Europe', 'students': 2, 'cases': 2}])
        self.assertEqual(sum(b['n'] for b in doc['turn_length']), 2)
        bins = {b['label']: b['n'] for b in doc['turn_length']}
        self.assertEqual(bins['5–9'], 2)
        self.assertEqual(bins['0–4'], 0)
        self.assertEqual(doc['meta']['practice_cases'], 4)
        self.assertFalse(doc['meta']['participants_only'])

    def test_participants_only(self):
        data = self.make(self.write_deidentified_logs())
        doc = data.document(True)
        self.assertEqual(doc['kpis']['cases'], 1)
        self.assertEqual([p['id'] for p in doc['progress']], ['aaaaaaaa'])
        self.assertEqual(doc['meta']['participants_listed'], 1)

    def test_listed_participants_without_cases_appear_with_zero(self):
        deid = self.write_deidentified_logs()
        (deid / 'participants.json').write_text(json.dumps({'participants': {'aaaaaaaa': 'p', 'cccccccc': 'p'}}))
        doc = self.make(deid).document(True)
        rows = {p['id']: p for p in doc['progress']}
        self.assertEqual(rows['cccccccc']['cases'], 0)
        self.assertEqual(doc['kpis']['participants_started'], 1)

    def test_practice_cases_are_not_real_cases(self):
        deid = self.write_deidentified_logs()
        self.assertEqual(self.make(deid).document(False)['kpis']['real_cases'], 0)
        self.assertEqual(self.make(deid, trial_case_count=0).document(False)['kpis']['real_cases'], 2)

    def test_participants_only_without_the_file(self):
        deid = self.write_deidentified_logs()
        (deid / 'participants.json').unlink()
        data = self.make(deid)
        self.assertFalse(data.participants_available)
        with self.assertRaises(ParticipantsUnavailable):
            data.document(True)
        self.assertEqual(data.document(False)['kpis']['cases'], 2)

    def test_cache_is_reused_until_a_file_changes(self):
        deid = self.write_deidentified_logs()
        data = self.make(deid)
        first = data.document(False)
        self.assertIs(data.document(False), first)
        with open(deid / 'usage.csv', 'a') as fd:
            fd.write('2026-10-03 14:00:00,aaaaaaaa,LOG_CONTENT,,\n')
        second = data.document(False)
        self.assertIsNot(second, first)
        self.assertIs(data.document(False), second)

    def test_raw_logs_are_refused(self):
        with self.assertRaises(IdentifyingDataError):
            self.make(self.raw).document(False)

    def test_empty_log_directory(self):
        empty = self.dir / 'empty'
        empty.mkdir()
        doc = self.make(empty).document(False)
        json.dumps(doc, allow_nan=False)
        self.assertEqual(doc['kpis']['cases'], 0)
        self.assertEqual(doc['progress'], [])
        self.assertIsNone(doc['meta']['data_through'])

    def test_model_performance_and_nan_become_json(self):
        deid = self.write_deidentified_logs()
        doc = self.make(deid).document(False)
        self.assertEqual(doc['models'][0]['model'], 'gpt-4o')
        self.assertEqual(doc['models'][0]['count'], 2)
        self.assertIsNone(doc['kpis']['guess_accuracy'] if not doc['kpis']['guesses'] else None)

    def test_jsonable(self):
        import numpy as np
        out = DashboardData._jsonable({'a': np.int64(3), 'b': np.float64('nan'), 'c': pd.NaT, 'd': pd.NA,
                                       'e': pd.Timestamp('2026-10-03 20:40:07'), 'f': np.bool_(True),
                                       'g': [float('inf'), 1.5]})
        self.assertEqual(out, {'a': 3, 'b': None, 'c': None, 'd': None, 'e': '2026-10-03T20:40:07',
                               'f': True, 'g': [None, 1.5]})


@unittest.skipIf(tornado is None, 'Tornado is not installed here')
class ServerTests(tornado.testing.AsyncHTTPTestCase if tornado else unittest.TestCase):

    USER = 'maria'
    PASSWORD = 'a long enough password'

    def get_app(self):
        helper = LogAnalysisTestBase()
        helper.setUp()
        self.addCleanup(helper.tmp.cleanup)
        self.deid = helper.write_deidentified_logs()
        self.raw = helper.raw
        auth = helper.dir / 'auth.txt'
        PasswordFile.create(self.USER, self.PASSWORD).save(auth)
        self.config = DashboardConfig(self.deid, auth_file=auth)
        patcher = mock.patch.object(SecurityMixin, 'FAILED_LOGIN_DELAY_SECS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        return DashboardServer(self.config).make_app()

    def login(self, user=None, password=None):
        token = base64.b64encode(f"{user or self.USER}:{password or self.PASSWORD}".encode()).decode()
        return {'Authorization': f'Basic {token}'}

    def test_everything_but_health_needs_the_login(self):
        for path in ('/', '/api/data', '/static/dashboard.js', '/static/vendor/d3.min.js'):
            resp = self.fetch(path)
            self.assertEqual(resp.code, 401, path)
            self.assertIn('Basic', resp.headers['WWW-Authenticate'])
        self.assertEqual(self.fetch('/healthz').code, 200)

    def test_wrong_and_malformed_logins(self):
        self.assertEqual(self.fetch('/', headers=self.login(password='nope nope nope nope')).code, 401)
        self.assertEqual(self.fetch('/', headers=self.login(user='someone')).code, 401)
        for header in ('Basic', 'Basic !!!notbase64', 'Bearer abc', 'Basic ' + base64.b64encode(b'nocolon').decode()):
            self.assertEqual(self.fetch('/', headers={'Authorization': header}).code, 401, header)

    def test_page_and_protective_headers(self):
        resp = self.fetch('/', headers=self.login())
        self.assertEqual(resp.code, 200)
        self.assertIn(b'Experiment progress', resp.body)
        csp = resp.headers['Content-Security-Policy']
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertEqual(resp.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(resp.headers['X-Frame-Options'], 'DENY')
        self.assertNotIn('Tornado', resp.headers.get('Server', ''))

    def test_static_files_and_no_traversal(self):
        self.assertEqual(self.fetch('/static/dashboard.js', headers=self.login()).code, 200)
        self.assertEqual(self.fetch('/static/vendor/d3.min.js', headers=self.login()).code, 200)
        for path in ('/static/../dashboard_server.py', '/static/%2e%2e/dashboard_server.py', '/static/nope.js'):
            self.assertIn(self.fetch(path, headers=self.login()).code, (403, 404), path)

    def test_data_document(self):
        resp = self.fetch('/api/data?participants_only=1', headers=self.login())
        self.assertEqual(resp.code, 200)
        self.assertTrue(resp.headers['Content-Type'].startswith('application/json'))
        self.assertEqual(resp.headers['Cache-Control'], 'no-store')
        doc = json.loads(resp.body)
        self.assertEqual(doc['kpis']['cases'], 1)
        self.assertEqual(json.loads(self.fetch('/api/data', headers=self.login()).body)['kpis']['cases'], 2)

    def test_participants_only_without_file_is_409(self):
        (self.deid / 'participants.json').unlink()
        resp = self.fetch('/api/data?participants_only=1', headers=self.login())
        self.assertEqual(resp.code, 409)
        self.assertIn('error', json.loads(resp.body))

    def test_raw_logs_are_never_served(self):
        self.config.log_dir = self.raw
        resp = self.fetch('/api/data', headers=self.login())
        self.assertEqual(resp.code, 500)
        self.assertNotIn('alice@example.org', resp.body.decode())

    def test_only_get(self):
        self.assertEqual(self.fetch('/api/data', method='POST', body='', headers=self.login()).code, 405)


@unittest.skipIf(tornado is None, 'Tornado is not installed here')
class NoAuthServerTests(tornado.testing.AsyncHTTPTestCase if tornado else unittest.TestCase):

    def get_app(self):
        helper = LogAnalysisTestBase()
        helper.setUp()
        self.addCleanup(helper.tmp.cleanup)
        return DashboardServer(DashboardConfig(helper.write_deidentified_logs(), no_auth=True)).make_app()

    def test_open_on_loopback_when_asked(self):
        self.assertEqual(self.fetch('/').code, 200)


@unittest.skipIf(tornado is None, 'Tornado is not installed here')
class MainTests(LogAnalysisTestBase):

    def test_bad_settings_exit_3(self):
        self.assertEqual(DashboardServer.main(['--log-dir', str(self.dir / 'nope')], {}), 3)

    def test_corrupt_password_file_exits_3(self):
        auth = self.dir / 'auth.txt'
        auth.write_text('garbage')
        self.assertEqual(DashboardServer.main(['--log-dir', str(self.write_deidentified_logs()),
                                               '--auth-file', str(auth)], {}), 3)


if __name__ == '__main__':
    unittest.main()
