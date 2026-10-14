# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/__tests__/test_evals_service.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:47:02
# **********************************************************

'''
Tests of the evaluation service: rubrics, credentials, the store, the roster,
bundles, the workflow, the administrator's tool, and (when Tornado is
installed) the HTTP API.

Run from the repository's src directory:

    python -m unittest evals_service.__tests__.test_evals_service
'''

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from evals_service.bundle_store import BundleStore
from evals_service.credentials import Credentials
from evals_service.eval_store import EvalStore, StoreError
from evals_service.evals_admin import EvalsAdmin
from evals_service.evals_common import EvalsCommon
from evals_service.roster import Roster
from evals_service.rubrics import RubricLibrary
from evals_service.workflow import AccessError, Workflow

try:
    import tornado.testing
    from evals_service.evals_server import EvalsServer, ServiceContext
    HAVE_TORNADO = True
except ImportError:                                          # pragma: no cover
    HAVE_TORNADO = False


class FakeClock:
    '''
    A settable clock returning aware datetimes.
    '''

    def __init__(self, start=None):
        self.now = start or datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class TestBase(unittest.TestCase):
    '''
    Temporary service root, inbox, clock, and bundle-making helpers.
    '''

    SESSION_HTML = ('<section class="ev-case"><header><h2>Maria</h2></header><div class="ev-turns">'
                    '<div class="ev-turn ev-client ev-ai"><div class="ev-who">Client</div>'
                    '<div class="ev-txt">I am fine.</div></div></div></section>')

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'evals'
        self.inbox = self.tmp / 'inbox'
        self.inbox.mkdir()
        self.clock = FakeClock()
        self.rubrics = RubricLibrary()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def session(n, code='aaaaaaaa', **extra):
        doc = {'session_id': f'{n:016x}', 'student_code': code, 'day': '2026-10-03',
               'created_utc': '2026-10-03T17:00:00Z', 'client_name': 'Maria', 'defense': 'projection',
               'html': TestBase.SESSION_HTML, 'critiques': ['A fair critique.']}
        doc.update(extra)
        return doc

    def make_bundle(self, sessions, bundle_id='20261101T000000Z', built='2026-11-01T00:00:00Z', **manifest_changes):
        raw = ''.join(json.dumps(s) + '\n' for s in sessions).encode('utf-8')
        manifest = {'format_version': EvalsCommon.BUNDLE_FORMAT_VERSION, 'bundle_id': bundle_id, 'built_utc': built,
                    'sessions_sha256': hashlib.sha256(raw).hexdigest(), 'session_count': len(sessions),
                    'role_colors': EvalsCommon.ROLE_COLORS}
        manifest.update(manifest_changes)
        (self.inbox / EvalsCommon.SESSIONS_FILE_FORMAT.format(bundle_id=bundle_id)).write_bytes(raw)
        (self.inbox / EvalsCommon.MANIFEST_FILE_FORMAT.format(bundle_id=bundle_id)).write_text(json.dumps(manifest))
        return manifest

    def full_answers(self, kind='session_review', value=7, na=False):
        answers = {'comments': {}}
        for section in self.rubrics.sections_of(kind):
            answers[section] = {}
        for section, item_id in self.rubrics.item_ids(kind):
            if section == 'student':
                answers[section][item_id] = value
            else:
                allowed = item_id not in ('vignette_quality', 'opening_defense')
                answers[section][item_id] = EvalsCommon.NA if (na and allowed) else 5
        return answers


# ---------------------------------------------------------------- rubrics

class RubricTests(TestBase):

    def test_loads_and_has_all_forms(self):
        self.assertEqual(set(self.rubrics.form_kinds()), set(EvalsCommon.ALL_FORMS))
        self.assertEqual(self.rubrics.sections_of('session_review'), ['student', 'trainer'])
        self.assertEqual(self.rubrics.sections_of('self_assessment'), ['student'])
        self.assertTrue(self.rubrics.is_blinded('session_review'))
        self.assertFalse(self.rubrics.is_blinded('instructor_assessment'))

    def test_item_counts(self):
        ids = self.rubrics.item_ids('session_review')
        self.assertEqual(sum(1 for s, _ in ids if s == 'student'), 8)
        self.assertEqual(sum(1 for s, _ in ids if s == 'trainer'), 7)

    def test_defense_is_shown_in_the_second_trainer_item(self):
        form = self.rubrics.form_for_display('session_review', defense='projection')
        trainer = next(s for s in form['sections'] if s['name'] == 'trainer')
        prompts = {i['id']: i['prompt'] for i in trainer['items']}
        self.assertIn('(projection)', prompts['opening_defense'])
        self.assertNotIn('{defense}', prompts['opening_defense'])
        form = self.rubrics.form_for_display('session_review')
        self.assertIn('(not recorded)', next(s for s in form['sections'] if s['name'] == 'trainer')['items'][1]['prompt'])

    def test_instructions_only_with_the_student_rubric(self):
        self.assertIsNotNone(self.rubrics.form_for_display('self_assessment')['instructions'])

    def test_complete_answers_pass(self):
        cleaned = self.rubrics.clean_answers('session_review', self.full_answers(), complete=True)
        self.assertEqual(cleaned['student']['anxiety'], 7)

    def test_scale_limits(self):
        answers = self.full_answers('self_assessment', 10)
        self.rubrics.clean_answers('self_assessment', answers, complete=True)
        answers['student']['anxiety'] = 11
        with self.assertRaisesRegex(ValueError, 'between 1 and 10'):
            self.rubrics.clean_answers('self_assessment', answers, complete=True)
        answers['student']['anxiety'] = 0
        with self.assertRaises(ValueError):
            self.rubrics.clean_answers('self_assessment', answers, complete=False)

    def test_trainer_scale_is_one_to_seven(self):
        answers = self.full_answers()
        answers['trainer']['vignette_quality'] = 8
        with self.assertRaisesRegex(ValueError, 'between 1 and 7'):
            self.rubrics.clean_answers('session_review', answers, complete=False)

    def test_na_only_where_allowed(self):
        answers = self.full_answers(na=True)
        self.rubrics.clean_answers('session_review', answers, complete=True)      # items 3-7 may be n/a
        answers['trainer']['vignette_quality'] = EvalsCommon.NA
        with self.assertRaisesRegex(ValueError, 'not allowed'):
            self.rubrics.clean_answers('session_review', answers, complete=True)
        answers = self.full_answers('self_assessment')
        answers['student']['anxiety'] = EvalsCommon.NA
        with self.assertRaises(ValueError):
            self.rubrics.clean_answers('self_assessment', answers, complete=True)

    def test_drafts_may_be_partial_but_submissions_may_not(self):
        partial = {'student': {'anxiety': 4}}
        self.assertEqual(self.rubrics.clean_answers('self_assessment', partial, complete=False)['student'], {'anxiety': 4})
        with self.assertRaisesRegex(ValueError, 'unanswered'):
            self.rubrics.clean_answers('self_assessment', partial, complete=True)

    def test_rejects_unknown_parts_bools_and_floats(self):
        for bad in ({'other': {}}, {'student': {'nope': 3}}, {'student': {'anxiety': True}},
                    {'student': {'anxiety': 3.5}}, {'student': {'anxiety': '5'}}, {'student': 'x'}, 'x'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.rubrics.clean_answers('self_assessment', bad, complete=False)

    def test_comments(self):
        answers = self.full_answers('self_assessment')
        answers['comments'] = {'student': '  good session  '}
        self.assertEqual(self.rubrics.clean_answers('self_assessment', answers, True)['comments'],
                         {'student': 'good session'})
        answers['comments'] = {'student': 'x' * (EvalsCommon.MAX_COMMENT_LEN + 1)}
        with self.assertRaisesRegex(ValueError, 'longer'):
            self.rubrics.clean_answers('self_assessment', answers, True)
        answers['comments'] = {'trainer': 'no such section here'}
        with self.assertRaises(ValueError):
            self.rubrics.clean_answers('self_assessment', answers, True)

    def test_version_changes_with_content(self):
        other = self.tmp / 'r.json'
        doc = json.loads(RubricLibrary.DEFAULT_PATH.read_text())
        doc['instructions']['title'] = 'Changed'
        other.write_text(json.dumps(doc))
        self.assertNotEqual(RubricLibrary(other).version, self.rubrics.version)

    def test_inconsistent_bands_are_refused(self):
        other = self.tmp / 'r.json'
        doc = json.loads(RubricLibrary.DEFAULT_PATH.read_text())
        doc['rubrics']['student']['items'][0]['bands'].pop()
        other.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'bands'):
            RubricLibrary(other)


# ---------------------------------------------------------------- credentials

class CredentialsTests(TestBase):

    def test_password_roundtrip(self):
        stored = Credentials.hash_password('correct horse battery')
        self.assertTrue(Credentials.verify_password('correct horse battery', stored))
        self.assertFalse(Credentials.verify_password('correct horse batterz', stored))
        self.assertFalse(Credentials.verify_password('x', None))
        self.assertFalse(Credentials.verify_password('x', 'garbage'))
        self.assertNotEqual(stored, Credentials.hash_password('correct horse battery'))   # salted

    def test_password_policy(self):
        Credentials.check_password_policy('long enough pw')
        with self.assertRaises(ValueError):
            Credentials.check_password_policy('short')
        with self.assertRaises(ValueError):
            Credentials.check_password_policy(None)

    def test_codes(self):
        code = Credentials.new_invite_code()
        self.assertRegex(code, r'^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$')
        self.assertEqual(Credentials.hash_code(code), Credentials.hash_code(code.lower().replace('-', ' ')))
        self.assertNotEqual(Credentials.hash_code(code), Credentials.hash_code('AAAA-AAAA-AAAA'))
        self.assertEqual(Credentials.normalize_code('o0-iI-lL'), '001111')

    def test_tokens(self):
        token, hashed = Credentials.new_device_token()
        self.assertEqual(Credentials.hash_token(token), hashed)
        self.assertNotEqual(token, Credentials.new_device_token()[0])
        self.assertGreaterEqual(len(token), 40)

    def test_email(self):
        self.assertEqual(Credentials.normalize_email('  Jon@Example.ORG '), 'jon@example.org')
        for bad in ('', 'x', 'a@b', 'a b@c.de', None):
            with self.assertRaises(ValueError):
                Credentials.normalize_email(bad)


# ---------------------------------------------------------------- store

class StoreTests(TestBase):

    def setUp(self):
        super().setUp()
        self.store = EvalStore(self.root / 'data' / 'evals.sqlite', clock=self.clock)

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_labels_for_evaluators_are_numbered(self):
        a = self.store.add_person('a@x.org', ['evaluator'])
        b = self.store.add_person('b@x.org', ['evaluator'])
        s = self.store.add_person('s@x.org', ['student'])
        self.assertEqual(self.store.person_by_id(a)['label'], 'Evaluator_1')
        self.assertEqual(self.store.person_by_id(b)['label'], 'Evaluator_2')
        self.assertEqual(self.store.person_by_id(s)['label'], 's@x.org')

    def test_explicit_label_and_duplicates(self):
        self.store.add_person('jon@x.org', ['evaluator'], label='Evaluator_1')
        with self.assertRaisesRegex(StoreError, 'already'):
            self.store.add_person('jon@x.org', ['evaluator'])
        with self.assertRaisesRegex(StoreError, 'label'):
            self.store.add_person('o@x.org', ['evaluator'], label='Evaluator_1')
        with self.assertRaises(StoreError):
            self.store.add_person('r@x.org', ['wizard'])

    def test_activation_consumes_the_invite(self):
        pid = self.store.add_person('a@x.org', ['evaluator'], invite_hash=Credentials.hash_code('K7M2-9QXP-4TBD'))
        person = self.store.person_by_id(pid)
        self.assertIsNotNone(person['invite_hash'])
        self.assertIsNone(person['activated_utc'])
        self.store.activate(pid, Credentials.hash_password('a long password'))
        person = self.store.person_by_id(pid)
        self.assertIsNone(person['invite_hash'])
        self.assertIsNotNone(person['activated_utc'])

    def test_device_tokens(self):
        pid = self.store.add_person('a@x.org', ['evaluator'])
        self.store.activate(pid, 'hash')
        token, hashed = Credentials.new_device_token()
        self.store.add_device_token(pid, hashed)
        self.assertEqual(self.store.person_for_token(hashed)['id'], pid)
        self.assertIsNone(self.store.person_for_token(Credentials.hash_token('wrong')))
        # use extends the life; disuse ends it
        self.clock.advance(days=EvalsCommon.DEVICE_TOKEN_VALID_DAYS - 1)
        self.assertIsNotNone(self.store.person_for_token(hashed))
        self.clock.advance(days=EvalsCommon.DEVICE_TOKEN_VALID_DAYS - 1)
        self.assertIsNotNone(self.store.person_for_token(hashed))
        self.clock.advance(days=EvalsCommon.DEVICE_TOKEN_VALID_DAYS + 1)
        self.assertIsNone(self.store.person_for_token(hashed))

    def test_disabled_accounts_lose_their_tokens(self):
        pid = self.store.add_person('a@x.org', ['evaluator'])
        self.store.activate(pid, 'hash')
        _, hashed = Credentials.new_device_token()
        self.store.add_device_token(pid, hashed)
        self.store.set_disabled(pid, True)
        self.assertIsNone(self.store.person_for_token(hashed))

    def test_draft_then_submit_then_frozen(self):
        pid = self.store.add_person('a@x.org', ['evaluator'])
        row = self.store.save_draft(pid, 'session_review', 's1', {'student': {'anxiety': 3}}, 'v1')
        self.assertEqual(row['state'], 'draft')
        row = self.store.save_draft(pid, 'session_review', 's1', {'student': {'anxiety': 4}}, 'v1')
        self.assertEqual(row['answers']['student']['anxiety'], 4)
        row = self.store.submit(pid, 'session_review', 's1', {'student': {'anxiety': 5}}, 'v1')
        self.assertEqual(row['state'], 'submitted')
        self.assertIsNotNone(row['submitted_utc'])
        with self.assertRaisesRegex(StoreError, 'already submitted'):
            self.store.save_draft(pid, 'session_review', 's1', {'student': {'anxiety': 6}}, 'v1')
        with self.assertRaises(StoreError):
            self.store.submit(pid, 'session_review', 's1', {'student': {'anxiety': 6}}, 'v1')
        self.assertEqual(self.store.get_rating(pid, 'session_review', 's1')['answers']['student']['anxiety'], 5)

    def test_database_itself_refuses_changes_to_submitted_ratings(self):
        import sqlite3
        pid = self.store.add_person('a@x.org', ['evaluator'])
        self.store.submit(pid, 'session_review', 's1', {'student': {'anxiety': 5}}, 'v1')
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'cannot be changed'):
            self.store._db.execute("UPDATE ratings SET answers_json='{}'")
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'cannot be deleted'):
            self.store._db.execute("DELETE FROM ratings")

    def test_ratings_are_per_person_form_and_subject(self):
        a = self.store.add_person('a@x.org', ['evaluator'])
        b = self.store.add_person('b@x.org', ['evaluator'])
        self.store.submit(a, 'session_review', 's1', {}, 'v1')
        self.store.save_draft(b, 'session_review', 's1', {}, 'v1')
        self.store.save_draft(a, 'self_assessment', 's1', {}, 'v1')
        self.assertEqual(len(self.store.ratings_of(a)), 2)
        self.assertEqual(len(self.store.ratings_of(a, 'session_review')), 1)
        self.assertEqual(len(self.store.ratings_of(None, 'session_review')), 2)

    def test_seen_and_export(self):
        a = self.store.add_person('a@x.org', ['evaluator'])
        self.store.mark_seen(a, 'session_review', 's1')
        self.store.mark_seen(a, 'session_review', 's1')
        self.assertEqual(self.store.seen_of(a), {'s1'})
        self.store.submit(a, 'session_review', 's1', {'student': {'anxiety': 5}}, 'v1')
        self.store.save_draft(a, 'session_review', 's2', {'student': {'anxiety': 5}}, 'v1')
        rows = self.store.export_submitted()
        self.assertEqual([r['subject_key'] for r in rows], ['s1'])
        self.assertEqual(rows[0]['rater_label'], 'Evaluator_1')

    def test_reopening_keeps_data(self):
        a = self.store.add_person('a@x.org', ['evaluator'])
        self.store.submit(a, 'session_review', 's1', {'student': {'anxiety': 5}}, 'v1')
        self.store.close()
        self.store = EvalStore(self.root / 'data' / 'evals.sqlite', clock=self.clock)
        self.assertEqual(self.store.get_rating(a, 'session_review', 's1')['state'], 'submitted')


# ---------------------------------------------------------------- roster

class RosterTests(TestBase):

    DOC = {'milestones': [{'id': 'm2', 'label': 'Second', 'opens': '2027-02-01'},
                          {'id': 'm1', 'label': 'First', 'opens': '2026-11-15'}],
           'cohorts': [{'id': 'A', 'instructors': ['Jon@X.org'],
                        'students': [{'email': 's1@x.org', 'name': 'One'}, {'email': 'S2@x.org'}]},
                       {'id': 'B', 'instructors': ['jon@x.org', 'kim@x.org'],
                        'students': [{'email': 's3@x.org', 'id': 'S77'}]}]}

    def test_parsing(self):
        roster = Roster(self.DOC)
        self.assertEqual([m.id for m in roster.milestones], ['m1', 'm2'])
        self.assertEqual({s.email for s in roster.students_taught_by('jon@x.org')}, {'s1@x.org', 's2@x.org', 's3@x.org'})
        self.assertEqual({s.email for s in roster.students_taught_by('kim@x.org')}, {'s3@x.org'})
        self.assertEqual(roster.student_by_email('s2@x.org').name, 's2')
        self.assertEqual(roster.student_by_id('S77').email, 's3@x.org')
        self.assertTrue(roster.student_by_email('s1@x.org').id.startswith('S'))

    def test_open_milestones(self):
        roster = Roster(self.DOC)
        self.assertEqual([m.id for m in roster.open_milestones(date(2026, 11, 14))], [])
        self.assertEqual([m.id for m in roster.open_milestones(date(2026, 11, 15))], ['m1'])
        self.assertEqual([m.id for m in roster.open_milestones(date(2027, 3, 1))], ['m1', 'm2'])

    def test_duplicates_are_refused(self):
        doc = json.loads(json.dumps(self.DOC))
        doc['cohorts'][1]['students'].append({'email': 's1@x.org'})
        with self.assertRaisesRegex(ValueError, 'twice'):
            Roster(doc)

    def test_missing_file_gives_empty_roster(self):
        roster = Roster.load(self.tmp / 'nope.json')
        self.assertEqual(roster.cohorts, [])

    def test_all_emails(self):
        kinds = {e: k for e, _, k in Roster(self.DOC).all_emails()}
        self.assertEqual(kinds['jon@x.org'], 'instructor')
        self.assertEqual(kinds['s1@x.org'], 'student')
        self.assertEqual(len(Roster(self.DOC).all_emails()), 5)


# ---------------------------------------------------------------- bundles

class BundleTests(TestBase):

    def store(self, **kw):
        return BundleStore(self.inbox, self.root, **kw)

    def test_no_bundle(self):
        bundles = self.store()
        self.assertFalse(bundles.refresh(force=True))
        self.assertIsNone(bundles.info())
        self.assertEqual(bundles.session_ids(), [])

    def test_installs_and_serves(self):
        self.make_bundle([self.session(1), self.session(2, 'bbbbbbbb')])
        bundles = self.store()
        self.assertTrue(bundles.refresh(force=True))
        self.assertEqual(bundles.info()['session_count'], 2)
        self.assertEqual(bundles.info()['participant_count'], 2)
        self.assertEqual(bundles.session(f'{1:016x}')['defense'], 'projection')
        self.assertTrue((self.root / 'releases' / 'current').is_symlink())
        self.assertTrue((self.root / 'releases' / '20261101T000000Z' / 'manifest-20261101T000000Z.json').exists())

    def test_a_restart_loads_the_installed_release(self):
        self.make_bundle([self.session(1)])
        self.store().refresh(force=True)
        shutil.rmtree(self.inbox)
        self.inbox.mkdir()
        again = self.store()
        self.assertEqual(again.session_ids(), [f'{1:016x}'])

    def test_manifest_is_the_commit_marker(self):
        self.make_bundle([self.session(1)])
        (self.inbox / 'manifest-20261101T000000Z.json').unlink()
        bundles = self.store()
        self.assertFalse(bundles.refresh(force=True))
        self.assertIsNone(bundles.info())

    def test_checksum_mismatch_is_refused(self):
        self.make_bundle([self.session(1)], sessions_sha256='0' * 64)
        bundles = self.store()
        with self.assertLogs('evals_service', 'ERROR'):
            self.assertFalse(bundles.refresh(force=True))
        self.assertIsNone(bundles.info())

    def test_wrong_format_version_and_colors_are_refused(self):
        for change in ({'format_version': 99}, {'role_colors': {'client': '#000000'}}, {'session_count': 5}):
            shutil.rmtree(self.inbox)
            self.inbox.mkdir()
            self.make_bundle([self.session(1)], **change)
            bundles = self.store()
            with self.assertLogs('evals_service', 'ERROR'):
                self.assertFalse(bundles.refresh(force=True), msg=str(change))

    def test_active_content_and_addresses_are_refused(self):
        cases = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '<a href="javascript:x">y</a>',
                 '<iframe src=x></iframe>', 'write to me@example.org']
        for text in cases:
            shutil.rmtree(self.inbox)
            self.inbox.mkdir()
            self.make_bundle([self.session(1, html=f'<section class="ev-case">{text}</section>')])
            with self.assertLogs('evals_service', 'ERROR'):
                self.assertFalse(self.store().refresh(force=True), msg=text)
        shutil.rmtree(self.inbox)
        self.inbox.mkdir()
        self.make_bundle([self.session(1, critiques=['mail me: x@y.org'])])
        with self.assertLogs('evals_service', 'ERROR'):
            self.assertFalse(self.store().refresh(force=True))

    def test_malformed_ids_are_refused(self):
        self.make_bundle([self.session(1, session_id='../../etc')])
        with self.assertLogs('evals_service', 'ERROR'):
            self.assertFalse(self.store().refresh(force=True))

    def test_newer_bundle_replaces_older_and_old_ones_are_pruned(self):
        bundles = self.store()
        for n in range(EvalsCommon.RELEASES_KEPT + 2):
            self.make_bundle([self.session(i) for i in range(1, n + 2)], bundle_id=f'b{n}',
                             built=f'2026-11-0{n + 1}T00:00:00Z')
            self.assertTrue(bundles.refresh(force=True))
            self.assertEqual(bundles.info()['bundle_id'], f'b{n}')
        releases = [p for p in (self.root / 'releases').iterdir() if p.is_dir() and not p.is_symlink()]
        self.assertEqual(len(releases), EvalsCommon.RELEASES_KEPT)

    def test_a_bad_newer_bundle_leaves_the_good_one_in_service(self):
        self.make_bundle([self.session(1)], bundle_id='good', built='2026-11-01T00:00:00Z')
        bundles = self.store()
        bundles.refresh(force=True)
        self.make_bundle([self.session(2)], bundle_id='bad', built='2026-11-02T00:00:00Z', sessions_sha256='1' * 64)
        with self.assertLogs('evals_service', 'ERROR'):
            self.assertFalse(bundles.refresh(force=True))
        self.assertEqual(bundles.info()['bundle_id'], 'good')
        # and the failure is not retried until the file changes
        self.assertFalse(bundles.refresh(force=True))

    def test_rescans_are_rate_limited(self):
        ticks = [0.0]
        bundles = self.store(clock=lambda: ticks[0])
        bundles.refresh()
        self.make_bundle([self.session(1)])
        ticks[0] = 1.0
        self.assertFalse(bundles.refresh())
        ticks[0] = EvalsCommon.BUNDLE_RESCAN_SECS + 1
        self.assertTrue(bundles.refresh())


# ---------------------------------------------------------------- workflow

class WorkflowTests(TestBase):

    ROSTER = {'milestones': [{'id': 'm1', 'label': 'After the first weekend', 'opens': '2026-11-15'},
                             {'id': 'm2', 'label': 'After the second weekend', 'opens': '2027-02-01'}],
              'cohorts': [{'id': 'A', 'instructors': ['jon@x.org'],
                           'students': [{'email': 's1@x.org', 'name': 'Student One', 'id': 'S1'},
                                        {'email': 's2@x.org', 'name': 'Student Two', 'id': 'S2'}]}]}

    def setUp(self):
        super().setUp()
        self.store = EvalStore(self.root / 'data' / 'evals.sqlite', clock=self.clock)
        self.make_bundle([self.session(n) for n in range(1, 7)])
        self.bundles = BundleStore(self.inbox, self.root)
        self.bundles.refresh(force=True)
        self.workflow = Workflow(self.store, Roster(self.ROSTER), self.bundles, self.rubrics, clock=self.clock)
        self.jon = self.person('jon@x.org', ['evaluator'])
        self.kim = self.person('kim@x.org', ['evaluator'])
        self.s1 = self.person('s1@x.org', ['student'])
        self.admin = self.person('admin@x.org', ['admin'])

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def person(self, email, roles):
        pid = self.store.add_person(email, roles)
        self.store.activate(pid, 'hash')
        return self.store.person_by_id(pid)

    def test_review_order_is_stable_per_person_and_differs_between_people(self):
        order = lambda p: self.workflow._order(p, self.bundles.session_ids())
        self.assertEqual(order(self.jon), order(self.jon))
        self.assertNotEqual(order(self.jon), order(self.kim))
        self.assertEqual(self.workflow.next_review(self.jon), order(self.jon)[0])

    def test_next_review_walks_through_everything_and_ends(self):
        seen = []
        for _ in range(6):
            sid = self.workflow.next_review(self.jon)
            seen.append(sid)
            self.store.submit(self.jon['id'], 'session_review', sid, {}, 'v')
        self.assertEqual(len(set(seen)), 6)
        self.assertIsNone(self.workflow.next_review(self.jon))

    def test_a_draft_is_offered_before_anything_new(self):
        first = self.workflow.next_review(self.jon)
        self.store.save_draft(self.jon['id'], 'session_review', f'{3:016x}', {}, 'v')
        self.assertEqual(self.workflow.next_review(self.jon), f'{3:016x}')

    def test_students_and_admins_cannot_review_sessions(self):
        for who in (self.s1, self.admin):
            with self.assertRaises(AccessError):
                self.workflow.next_review(who)
            with self.assertRaises(AccessError):
                self.workflow.resolve(who, 'session_review', f'{1:016x}')

    def test_resolve_session(self):
        info = self.workflow.resolve(self.jon, 'session_review', f'{1:016x}')
        self.assertEqual(info['defense'], 'projection')
        self.assertEqual(info['subject']['kind'], 'session')
        self.assertNotIn('student_code', json.dumps(info))        # blinded
        with self.assertRaises(AccessError):
            self.workflow.resolve(self.jon, 'session_review', 'f' * 16)

    def test_assessments_open_with_their_milestone(self):
        self.assertEqual(len(self.workflow.overview(self.jon)['assessments']), 2)      # m1 opened 2026-11-15; today 11-20
        self.clock.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertEqual(self.workflow.overview(self.jon)['assessments'], [])
        with self.assertRaises(AccessError):
            self.workflow.resolve(self.jon, 'instructor_assessment', 'm1|S1')
        self.clock.now = datetime(2027, 3, 1, tzinfo=timezone.utc)
        self.assertEqual(len(self.workflow.overview(self.jon)['assessments']), 4)

    def test_only_the_students_instructor_may_assess(self):
        self.workflow.resolve(self.jon, 'instructor_assessment', 'm1|S1')
        with self.assertRaises(AccessError):
            self.workflow.resolve(self.kim, 'instructor_assessment', 'm1|S1')
        with self.assertRaises(AccessError):
            self.workflow.resolve(self.s1, 'instructor_assessment', 'm1|S1')

    def test_self_assessment_only_for_oneself(self):
        info = self.workflow.resolve(self.s1, 'self_assessment', 'm1|S1')
        self.assertEqual(info['subject']['name'], 'Student One')
        with self.assertRaises(AccessError):
            self.workflow.resolve(self.s1, 'self_assessment', 'm1|S2')
        with self.assertRaises(AccessError):
            self.workflow.resolve(self.jon, 'self_assessment', 'm1|S1')
        tasks = self.workflow.overview(self.s1)['assessments']
        self.assertEqual([t['form'] for t in tasks], ['self_assessment'])

    def test_bad_keys_and_forms(self):
        for kind, key in (('instructor_assessment', 'nonsense'), ('instructor_assessment', 'm9|S1'),
                          ('self_assessment', 'm1|S9'), ('wat', 'x')):
            with self.assertRaises(AccessError):
                self.workflow.resolve(self.jon, kind, key)

    def test_task_states_follow_the_ratings(self):
        self.store.save_draft(self.jon['id'], 'instructor_assessment', 'm1|S1', {}, 'v')
        self.store.submit(self.jon['id'], 'instructor_assessment', 'm1|S2', {}, 'v')
        states = {t['subject']: t['state'] for t in self.workflow.overview(self.jon)['assessments']}
        self.assertEqual(states, {'m1|S1': 'draft', 'm1|S2': 'submitted'})

    def test_progress_report(self):
        sid = self.workflow.next_review(self.jon)
        self.store.mark_seen(self.jon['id'], 'session_review', sid)
        self.store.submit(self.jon['id'], 'session_review', sid, {}, 'v')
        self.store.mark_seen(self.jon['id'], 'session_review', f'{9:016x}')
        report = self.workflow.progress()
        rows = {r['email']: r for r in report['rows']}
        self.assertEqual(rows['jon@x.org']['reviews']['submitted'], 1)
        self.assertEqual(rows['jon@x.org']['reviews']['available'], 6)
        self.assertEqual(rows['jon@x.org']['reviews']['remaining'], 5)
        self.assertEqual(rows['jon@x.org']['assessments']['expected'], 2)
        self.assertIsNone(rows['s1@x.org']['reviews'])
        self.assertEqual(rows['s1@x.org']['assessments']['expected'], 1)
        self.assertEqual(report['bundle']['session_count'], 6)
        self.assertEqual(rows['kim@x.org']['label'], 'Evaluator_2')

    def test_reminders(self):
        rows = lambda: {r['email']: r for r in self.workflow.progress()['rows']}
        self.assertFalse(rows()['jon@x.org']['needs_reminder'])
        self.clock.advance(days=EvalsCommon.REMINDER_AFTER_DAYS + 1)
        self.assertTrue(rows()['jon@x.org']['needs_reminder'])       # work outstanding, idle
        self.assertFalse(rows()['admin@x.org']['needs_reminder'])    # nothing outstanding
        self.store.touch(self.jon['id'])
        self.assertFalse(rows()['jon@x.org']['needs_reminder'])
        newcomer = self.store.add_person('new@x.org', ['evaluator'], invite_hash='h')
        self.assertTrue(rows()['new@x.org']['needs_reminder'])       # never logged in
        self.assertTrue(rows()['new@x.org']['invite_pending'])
        self.store.set_disabled(newcomer, True)
        self.assertFalse(rows()['new@x.org']['needs_reminder'])


# ---------------------------------------------------------------- admin tool

class AdminTests(TestBase):

    def setUp(self):
        super().setUp()
        self.out = io.StringIO()
        self.admin = EvalsAdmin(self.root, self.out)
        (self.root / 'config').mkdir(parents=True, exist_ok=True)
        roster_doc = json.loads(json.dumps(WorkflowTests.ROSTER))
        roster_doc['milestones'][0]['opens'] = '2026-01-01'      # open for the real clock
        (self.root / 'config' / 'roster.json').write_text(json.dumps(roster_doc))

    def tearDown(self):
        self.admin.store.close()
        super().tearDown()

    def test_invite_and_reissue(self):
        code = self.admin.invite('Jon@X.org', ['evaluator'], name='Jon', label='Evaluator_1')
        person = self.admin.store.person_by_email('jon@x.org')
        self.assertEqual(person['label'], 'Evaluator_1')
        self.assertEqual(person['invite_hash'], Credentials.hash_code(code))
        newer = self.admin.invite('jon@x.org', ['evaluator'])
        self.assertNotEqual(code, newer)
        self.assertEqual(self.admin.store.person_by_email('jon@x.org')['invite_hash'], Credentials.hash_code(newer))
        self.admin.store.activate(person['id'], 'hash')
        with self.assertRaisesRegex(StoreError, 'reissue'):
            self.admin.invite('jon@x.org', ['evaluator'])
        self.assertTrue(self.admin.invite('jon@x.org', ['evaluator'], reissue=True))

    def test_import_roster_creates_accounts_once(self):
        created = self.admin.import_roster()
        self.assertEqual({c['email']: c['roles'] for c in created},
                         {'jon@x.org': 'evaluator', 's1@x.org': 'student', 's2@x.org': 'student'})
        self.assertEqual({c['email']: c['label'] for c in created}['jon@x.org'], 'Evaluator_1')
        self.assertEqual(self.admin.import_roster(), [])

    def test_disable_and_listing(self):
        self.admin.invite('a@x.org', ['evaluator'])
        self.admin.set_disabled('a@x.org', True)
        self.admin.list_people()
        self.assertIn('disabled', self.out.getvalue())
        with self.assertRaises(StoreError):
            self.admin.set_disabled('nobody@x.org', True)

    def test_command_line(self):
        code = EvalsAdmin.main(['--service-root', str(self.root), 'invite', '--email', 'b@x.org', '--roles', 'admin,evaluator'],
                               out=self.out)
        self.assertEqual(code, 0)
        self.assertRegex(self.out.getvalue(), r'Invite code for b@x.org: [0-9A-Z]{4}-')
        person = self.admin.store.person_by_email('b@x.org')
        self.assertEqual(person['roles'], 'admin,evaluator')


# ---------------------------------------------------------------- HTTP API

@unittest.skipUnless(HAVE_TORNADO, 'Tornado is not installed')
class ServerTests(tornado.testing.AsyncHTTPTestCase if HAVE_TORNADO else unittest.TestCase):

    def get_app(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'evals'
        self.inbox = self.tmp / 'inbox'
        self.inbox.mkdir()
        helper = TestBase()
        helper.inbox = self.inbox
        self.helper = helper
        helper.make_bundle([TestBase.session(n) for n in range(1, 4)])
        (self.root / 'config').mkdir(parents=True)
        roster_doc = json.loads(json.dumps(WorkflowTests.ROSTER))
        roster_doc['milestones'][0]['opens'] = '2026-01-01'      # open for the real clock
        (self.root / 'config' / 'roster.json').write_text(json.dumps(roster_doc))
        self.ctx = ServiceContext(self.root, self.inbox)
        self.rubrics = self.ctx.rubrics
        self.admin = EvalsAdmin.__new__(EvalsAdmin)
        self.admin.root, self.admin.out, self.admin.store = self.root, io.StringIO(), self.ctx.store
        return EvalsServer.make_app(self.ctx)

    def tearDown(self):
        super().tearDown()
        self.ctx.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers

    def call(self, method, path, body=None, token=None, raw=False):
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        response = self.fetch(path, method=method, headers=headers,
                              body=None if body is None else json.dumps(body), raise_error=False)
        if raw:
            return response
        return response.code, (json.loads(response.body) if response.body else {})

    def register(self, email, roles, name=None, label=None, password='a long password!'):
        code = self.admin.invite(email, roles, name=name, label=label)
        status, doc = self.call('POST', '/api/register', {'email': email, 'code': code, 'password': password})
        self.assertEqual(status, 200, doc)
        return doc['token']

    def answers(self, kind='session_review'):
        return self._full(kind)

    def _full(self, kind):
        rubrics = self.rubrics
        answers = {'comments': {}}
        for section in rubrics.sections_of(kind):
            answers[section] = {}
        for section, item_id in rubrics.item_ids(kind):
            answers[section][item_id] = 7 if section == 'student' else 5
        return answers

    # tests

    def test_health_and_headers(self):
        response = self.call('GET', '/healthz', raw=True)
        self.assertEqual(response.body, b'ok')
        index = self.call('GET', '/', raw=True)
        self.assertEqual(index.code, 200)
        self.assertIn("script-src 'self'", index.headers['Content-Security-Policy'])
        self.assertEqual(index.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn(b'app.mjs', index.body)

    def test_static_files_and_module_type(self):
        js = self.call('GET', '/static/app.mjs', raw=True)
        self.assertEqual(js.code, 200)
        self.assertTrue(js.headers['Content-Type'].startswith('text/javascript'))
        self.assertEqual(self.call('GET', '/static/../evals_server.py', raw=True).code, 403)
        self.assertEqual(self.call('GET', '/static/nothing.js', raw=True).code, 404)

    def test_api_needs_a_login(self):
        for method, path in (('GET', '/api/me'), ('GET', '/api/review/next'), ('GET', '/api/item?form=x&subject=y'),
                             ('PUT', '/api/rating'), ('GET', '/api/admin/progress'), ('GET', '/api/admin/export.csv')):
            status, doc = self.call(method, path, {} if method == 'PUT' else None)
            self.assertEqual(status, 401, path)
        self.assertEqual(self.call('GET', '/api/me', token='bogus')[0], 401)

    def test_registration_with_invite_code(self):
        code = self.admin.invite('jon@x.org', ['evaluator'], name='Jon', label='Evaluator_1')
        status, _ = self.call('POST', '/api/register', {'email': 'jon@x.org', 'code': 'AAAA-AAAA-AAAA', 'password': 'a long password!'})
        self.assertEqual(status, 403)
        status, _ = self.call('POST', '/api/register', {'email': 'jon@x.org', 'code': code, 'password': 'short'})
        self.assertEqual(status, 400)
        status, doc = self.call('POST', '/api/register', {'email': 'JON@x.org', 'code': code.lower(), 'password': 'a long password!'})
        self.assertEqual(status, 200)
        self.assertEqual(doc['me']['label'], 'Evaluator_1')
        status, doc2 = self.call('GET', '/api/me', token=doc['token'])
        self.assertEqual(status, 200)
        self.assertEqual(doc2['me']['email'], 'jon@x.org')
        # the code works once
        status, _ = self.call('POST', '/api/register', {'email': 'jon@x.org', 'code': code, 'password': 'another long one!'})
        self.assertEqual(status, 403)

    def test_expired_invite(self):
        code = self.admin.invite('jon@x.org', ['evaluator'])
        self.ctx.store.set_invite(self.ctx.store.person_by_email('jon@x.org')['id'], Credentials.hash_code(code), valid_days=-1)
        status, _ = self.call('POST', '/api/register', {'email': 'jon@x.org', 'code': code, 'password': 'a long password!'})
        self.assertEqual(status, 403)

    def test_login_logout_and_second_browser(self):
        first = self.register('jon@x.org', ['evaluator'])
        status, doc = self.call('POST', '/api/login', {'email': 'jon@x.org', 'password': 'a long password!'})
        self.assertEqual(status, 200)
        second = doc['token']
        self.assertNotEqual(first, second)
        self.assertEqual(self.call('POST', '/api/logout', {}, token=second)[0], 200)
        self.assertEqual(self.call('GET', '/api/me', token=second)[0], 401)
        self.assertEqual(self.call('GET', '/api/me', token=first)[0], 200)     # the other browser stays in
        status, _ = self.call('POST', '/api/login', {'email': 'jon@x.org', 'password': 'wrong password!!'})
        self.assertEqual(status, 403)
        status, _ = self.call('POST', '/api/login', {'email': 'nobody@x.org', 'password': 'a long password!'})
        self.assertEqual(status, 403)

    def test_throttle(self):
        self.register('jon@x.org', ['evaluator'])
        for _ in range(EvalsCommon.LOGIN_MAX_FAILURES):
            self.assertEqual(self.call('POST', '/api/login', {'email': 'jon@x.org', 'password': 'wrong password!!'})[0], 403)
        self.assertEqual(self.call('POST', '/api/login', {'email': 'jon@x.org', 'password': 'a long password!'})[0], 429)

    def test_disabled_account_is_locked_out(self):
        token = self.register('jon@x.org', ['evaluator'])
        self.admin.set_disabled('jon@x.org', True)
        self.assertEqual(self.call('GET', '/api/me', token=token)[0], 401)
        self.assertEqual(self.call('POST', '/api/login', {'email': 'jon@x.org', 'password': 'a long password!'})[0], 403)

    def test_review_flow_draft_submit_and_freeze(self):
        token = self.register('kim@x.org', ['evaluator'])
        status, doc = self.call('GET', '/api/review/next', token=token)
        sid = doc['subject']
        self.assertIn(sid, self.ctx.bundles.session_ids())
        status, item = self.call('GET', f'/api/item?form=session_review&subject={sid}', token=token)
        self.assertEqual(status, 200)
        self.assertIsNone(item['rating'])
        self.assertEqual(item['key'], sid)
        self.assertIn('projection', json.dumps(item['form']))
        self.assertEqual(item['subject']['critiques'], ['A fair critique.'])
        self.assertNotIn('student_code', json.dumps(item))

        partial = {'form': 'session_review', 'subject': sid, 'answers': {'student': {'anxiety': 4}}}
        self.assertEqual(self.call('PUT', '/api/rating', partial, token)[0], 200)
        status, item = self.call('GET', f'/api/item?form=session_review&subject={sid}', token=token)
        self.assertEqual(item['rating']['state'], 'draft')
        self.assertEqual(item['rating']['answers']['student'], {'anxiety': 4})
        self.assertEqual(self.call('GET', '/api/review/next', token=token)[1]['subject'], sid)    # draft first

        status, doc = self.call('POST', '/api/rating/submit', partial, token)
        self.assertEqual(status, 400)
        self.assertIn('unanswered', doc['error'])
        full = {'form': 'session_review', 'subject': sid, 'answers': self.answers()}
        self.assertEqual(self.call('POST', '/api/rating/submit', full, token)[0], 200)
        self.assertEqual(self.call('PUT', '/api/rating', full, token)[0], 409)
        self.assertEqual(self.call('POST', '/api/rating/submit', full, token)[0], 409)
        self.assertNotEqual(self.call('GET', '/api/review/next', token=token)[1]['subject'], sid)

        status, me = self.call('GET', '/api/me', token=token)
        self.assertEqual(me['overview']['reviews']['submitted'], 1)

    def test_validation_errors(self):
        token = self.register('kim@x.org', ['evaluator'])
        sid = self.call('GET', '/api/review/next', token=token)[1]['subject']
        bad = {'form': 'session_review', 'subject': sid, 'answers': {'student': {'anxiety': 99}}}
        self.assertEqual(self.call('PUT', '/api/rating', bad, token)[0], 400)
        self.assertEqual(self.call('PUT', '/api/rating', {'form': 'session_review', 'subject': 'f' * 16, 'answers': {}}, token)[0], 404)
        response = self.fetch('/api/rating', method='PUT', body='not json', headers={'Authorization': f'Bearer {token}'},
                              raise_error=False)
        self.assertEqual(response.code, 400)

    def test_students_cannot_open_sessions_or_other_peoples_assessments(self):
        student = self.register('s1@x.org', ['student'])
        sid = self.ctx.bundles.session_ids()[0]
        self.assertEqual(self.call('GET', f'/api/item?form=session_review&subject={sid}', token=student)[0], 404)
        self.assertEqual(self.call('GET', '/api/review/next', token=student)[0], 403)
        self.assertEqual(self.call('GET', '/api/item?form=self_assessment&subject=m1|S2', token=student)[0], 404)
        status, item = self.call('GET', '/api/item?form=self_assessment&subject=m1|S1', token=student)
        self.assertEqual(status, 200)
        self.assertEqual(item['subject']['name'], 'Student One')
        full = {'form': 'self_assessment', 'subject': 'm1|S1', 'answers': self._full('self_assessment')}
        self.assertEqual(self.call('POST', '/api/rating/submit', full, student)[0], 200)

    def test_instructor_assessment_is_unblinded(self):
        jon = self.register('jon@x.org', ['evaluator'])
        status, me = self.call('GET', '/api/me', token=jon)
        titles = [t['title'] for t in me['overview']['assessments']]
        self.assertIn('Student One: After the first weekend', titles)
        status, item = self.call('GET', '/api/item?form=instructor_assessment&subject=m1|S1', token=jon)
        self.assertEqual(item['subject']['name'], 'Student One')
        self.assertFalse(item['form']['blinded'])

    def test_admin_endpoints(self):
        jon = self.register('jon@x.org', ['evaluator'])
        boss = self.register('boss@x.org', ['admin'], name='The Boss')
        self.assertEqual(self.call('GET', '/api/admin/progress', token=jon)[0], 403)
        self.assertEqual(self.call('GET', '/api/admin/export.csv', token=jon)[0], 403)
        status, report = self.call('GET', '/api/admin/progress', token=boss)
        self.assertEqual(status, 200)
        emails = {r['email'] for r in report['rows']}
        self.assertEqual(emails, {'jon@x.org', 'boss@x.org'})

        sid = self.call('GET', '/api/review/next', token=jon)[1]['subject']
        full = {'form': 'session_review', 'subject': sid, 'answers': self.answers()}
        self.assertEqual(self.call('POST', '/api/rating/submit', full, jon)[0], 200)
        csv_response = self.call('GET', '/api/admin/export.csv', token=boss, raw=True)
        self.assertEqual(csv_response.code, 200)
        text = csv_response.body.decode()
        self.assertTrue(text.startswith('rater,rater_email,form,subject,section,item,value'))
        self.assertIn(f'Evaluator_1,jon@x.org,session_review,{sid},student,anxiety,7', text)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith('Evaluator_1')]), 15)

    def test_new_bundle_is_picked_up_without_restart(self):
        token = self.register('kim@x.org', ['evaluator'])
        self.assertEqual(self.call('GET', '/api/me', token=token)[1]['bundle']['session_count'], 3)
        self.helper.make_bundle([TestBase.session(n) for n in range(1, 8)], bundle_id='newer', built='2026-12-01T00:00:00Z')
        self.ctx.bundles.refresh(force=True)
        self.assertEqual(self.call('GET', '/api/me', token=token)[1]['overview']['reviews']['available'], 7)


if __name__ == '__main__':
    unittest.main()
