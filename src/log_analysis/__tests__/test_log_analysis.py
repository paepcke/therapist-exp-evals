# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-10 18:47:48
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_analysis/__tests__/test_log_analysis.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:52:20
# **********************************************************

'''
Tests for log_analysis: reading de-identified logs, the participants
filter, the origin report and the command line. All data is synthetic.

The central test analyzes the same logs raw and de-identified and
requires the statistics to agree.
'''

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from log_analysis.log_analysis import AnalysisResult, LogAnalyzer, OriginReport, ParticipantFilter
from log_analysis.log_utils import LogUtils, TextCounts


class LogAnalysisTestBase(unittest.TestCase):

    @staticmethod
    def usage_zone_available():
        '''The usage log's zone name is a tz-database alias that minimal systems lack.'''
        try:
            ZoneInfo(LogUtils.USAGE_LOG_TZ)
            return True
        except ZoneInfoNotFoundError:
            return False

    ALICE = 'alice@example.org'
    BOB = 'bob@example.org'
    TESTER = 'tester@example.org'
    USAGE_HEADER = 'date,email,action,model,runtime'
    CRITIQUE_HTML = '<div><h1>Session Critique</h1><p>Made   progress &amp; more.</p></div>'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.raw = self.dir / 'raw'
        self.raw.mkdir()
        self.write_raw_logs()
        self.packs = self.dir / 'experiment_packs.json'
        self.packs.write_text(json.dumps({
            'default_pack': 'p1', 'packs': {'p1': {}, 'p2': {}},
            'participants': {self.ALICE: 'p1', self.BOB.upper(): 'p2'}}))

    @staticmethod
    def key(case, email):
        return f'{case}_{email}'

    def case_lines(self, case, email, tz, with_legacy=False):
        k = self.key(case, email)
        j = lambda **f: json.dumps({k: f})   # noqa: E731
        lines = [
            j(name='Anna', vignette='A fictional vignette of eight words.', defense='vagueness',
              ptSays='It is rough.', aiAlt='Another idea', datetimeUTC='2026-10-03T20:40:07.231Z',
              clientTimezone=tz),
            j(studentRole='student_is_therapist', dialogRole='therapist', speaker='human',
              content='Could we go back to the curfew moment?', datetimeUTC='2026-10-03T20:41:00.000Z'),
            j(studentRole='student_is_therapist', dialogRole='client', speaker='AI',
              content='Sometimes it is just rough.', datetimeUTC='2026-10-03T20:41:30.000Z'),
            j(defenseGuess='vagueness', datetimeUTC='2026-10-03T20:43:28.954Z'),
            j(event='roleSwitch', fromRole='student_is_therapist', toRole='questioning',
              datetimeUTC='2026-10-03T20:49:09.279Z'),
            j(studentRole='questioning', dialogRole='therapist', speaker='human',
              content='Seeded line', datetimeUTC='2026-10-03T20:49:09.900Z'),
            j(event='sessionCritiqueRequested', datetimeUTC='2026-10-03T20:53:30.000Z'),
            j(event='sessionCritiqueResult', ok=True, content=self.CRITIQUE_HTML,
              datetimeUTC='2026-10-03T20:53:39.463Z'),
            ]
        if with_legacy:
            lines.append(j(studentInitialResponse='old style text here'))
            lines.append(j(aiDiscussion='more old text'))
        return lines

    def write_raw_logs(self):
        h = self.USAGE_HEADER
        (self.raw / 'usage.csv').write_text('\n'.join([
            h,
            '2026-10-03 13:30:00,None,LOGIN,,',
            f'2026-10-03 13:31:00,{self.ALICE},EXPERIMENT_PACK_P1,,',
            f'2026-10-03 13:40:08,{self.ALICE},COMPLETE_NEW_CASE,gpt-4o,2.5',
            f'2026-10-03 13:41:00,{self.ALICE},LOG_CONTENT,,',
            f'2026-10-03 13:42:00,{self.BOB},COMPLETE_NEW_CASE,gpt-4o,3.5',
            f'2026-10-03 13:43:00,{self.TESTER},COMPLETE_NEW_CASE,gpt-4o,1.5',
            ]) + '\n')
        content = (self.case_lines('anna-2026-10-03T20-40-07-aaaaaa', self.ALICE, 'America/Los_Angeles', True)
                   + self.case_lines('bert-2026-10-03T20-40-07-bbbbbb', self.BOB, 'Europe/Berlin')
                   + self.case_lines('cara-2026-10-03T20-40-07-cccccc', self.TESTER, 'America/New_York'))
        (self.raw / 'content.jsonl').write_text('\n'.join(content) + '\n')

    DEID_USAGE = (
        'date,email,action,model,runtime\n'
        '2026-10-03 13:30:00,None,LOGIN,,\n'
        '2026-10-03 13:31:00,aaaaaaaa,EXPERIMENT_PACK_P1,,\n'
        '2026-10-03 13:40:08,aaaaaaaa,COMPLETE_NEW_CASE,gpt-4o,2.5\n'
        '2026-10-03 13:42:00,bbbbbbbb,COMPLETE_NEW_CASE,gpt-4o,3.5\n')

    def write_deidentified_logs(self):
        '''Logs in the format log_deid writes: codes for emails, counts for text.'''
        deid = self.dir / 'deid'
        deid.mkdir()
        (deid / 'usage.csv').write_text(self.DEID_USAGE)
        recs = []
        for code, case in (('aaaaaaaa', 'anna-2026-10-03T20-40-07-aaaaaa'), ('bbbbbbbb', 'bert-2026-10-03T20-40-07-bbbbbb')):
            k = f'{case}_{code}'
            recs += [
                {k: {'name': 'Anna', 'defense': 'vagueness', 'clientTimezone': 'Europe/Berlin',
                     'datetimeUTC': '2026-10-03T20:40:07.231Z', 'vignetteChars': 40, 'vignetteWords': 7,
                     'ptSaysChars': 12, 'ptSaysWords': 3, 'aiAltChars': 13, 'aiAltWords': 2}},
                {k: {'studentRole': 'student_is_therapist', 'dialogRole': 'therapist', 'speaker': 'human',
                     'datetimeUTC': '2026-10-03T20:41:00.000Z', 'contentChars': 39, 'contentWords': 8}},
                {k: {'event': 'sessionCritiqueResult', 'ok': True, 'datetimeUTC': '2026-10-03T20:53:39.463Z',
                     'contentChars': 38, 'contentWords': 6}},
                {k: {'studentInitialResponseChars': 19, 'studentInitialResponseWords': 4}},
                ]
        (deid / 'content.jsonl').write_text('\n'.join(json.dumps(r) for r in recs) + '\n')
        (deid / 'participants.json').write_text(json.dumps({'participants': {'aaaaaaaa': 'p1'}}))
        return deid


class TestTextCounts(unittest.TestCase):

    def test_from_text_and_from_counts(self):
        self.assertEqual(TextCounts.of({'content': 'a bc'}, 'content'), (4, 2))
        self.assertEqual(TextCounts.of({'contentChars': 4, 'contentWords': 2}, 'content'), (4, 2))
        self.assertEqual(TextCounts.of({}, 'content'), (None, None))
        self.assertEqual(TextCounts.of({'content': '<p>a  b</p>'}, 'content', is_html=True), (3, 2))

    def test_non_integer_counts_are_absent(self):
        self.assertEqual(TextCounts.of({'contentChars': 'x', 'contentWords': True}, 'content'), (None, None))

    def test_has(self):
        self.assertTrue(TextCounts.has({'vignette': 'x'}, 'vignette'))
        self.assertTrue(TextCounts.has({'vignetteChars': 1}, 'vignette'))
        self.assertFalse(TextCounts.has({'name': 'x'}, 'vignette'))


class TestParticipantFilter(LogAnalysisTestBase):

    def test_load_experiment_packs_normalizes(self):
        self.assertEqual(ParticipantFilter.load(self.packs), frozenset({self.ALICE, self.BOB}))

    def test_load_list_form(self):
        p = self.dir / 'p.json'
        p.write_text(json.dumps({'participants': [' A@x.org ', 'None', '']}))
        self.assertEqual(ParticipantFilter.load(p), frozenset({'a@x.org'}))

    def test_load_without_participants_is_an_error(self):
        p = self.dir / 'p.json'
        p.write_text(json.dumps({'packs': {}}))
        with self.assertRaises(ValueError):
            ParticipantFilter.load(p)

    def test_apply_drops_unidentified_rows(self):
        df = pd.DataFrame({'email': [self.ALICE, 'None', None, 'Bob@Example.org', self.TESTER], 'v': range(5)})
        kept, dropped = ParticipantFilter.apply(df, frozenset({self.ALICE, self.BOB}))
        self.assertEqual(list(kept['v']), [0, 3])
        self.assertEqual(dropped, 3)

    def test_apply_to_empty_frame(self):
        df = pd.DataFrame(columns=['email'])
        self.assertEqual(ParticipantFilter.apply(df, frozenset({'a'}))[1], 0)


class TestLogAnalyzer(LogAnalysisTestBase):

    def analyze(self, logdir=None, **kwargs):
        return LogAnalyzer(logdir or self.raw, **kwargs).analyze()

    def test_raw_logs_basics_are_unchanged(self):
        res = self.analyze()
        self.assertIsInstance(res, AnalysisResult)
        cases = res['cases']
        self.assertEqual(len(cases), 3)
        row = cases[cases['email'] == self.ALICE].iloc[0]
        self.assertEqual(row['n_student_turns'], 1)          # the seeded turn does not count
        self.assertEqual(row['n_critique_ok'], 1)
        if self.usage_zone_available():
            self.assertEqual(row['experiment_pack'], 'P1')
        self.assertEqual(row['vignette_words'], 6)
        self.assertEqual(row['student_initial_response_chars'], 19)
        turn = res['turns'].iloc[0]
        self.assertEqual(turn['content_words'], 8)
        crit = res['critiques'].iloc[0]
        self.assertEqual(crit['text_chars'], len('Session Critique Made progress & more.'))
        self.assertEqual(res.unrecognized, [])

    def test_participants_filter_on_raw_logs(self):
        res = self.analyze(participants=ParticipantFilter.load(self.packs))
        self.assertEqual(set(res['cases']['email']), {self.ALICE, self.BOB})
        self.assertEqual(set(res['usage_clean']['email']), {self.ALICE, self.BOB})   # LOGIN/None and tester gone
        self.assertEqual(set(res['turns']['email']), {self.ALICE, self.BOB})
        self.assertEqual(set(res['events']['email']), {self.ALICE, self.BOB})
        self.assertIn('Participants only (2 listed)', res.report)
        self.assertNotIn('REMINDER', res.report)

    def test_reads_the_deidentified_format(self):
        res = self.analyze(self.write_deidentified_logs())
        self.assertEqual(res.unrecognized, [])
        self.assertEqual(len(res['cases']), 2)
        row = res['cases'].iloc[0]
        self.assertEqual((row['vignette_chars'], row['vignette_words']), (40, 7))
        self.assertEqual((row['pt_says_chars'], row['ai_alt_words']), (12, 2))
        self.assertEqual(row['student_initial_response_chars'], 19)
        self.assertEqual(row['n_student_turns'], 1)
        turn = res['turns'].iloc[0]
        self.assertEqual((turn['content_chars'], turn['content_words']), (39, 8))
        self.assertTrue(pd.isna(turn['content']))
        crit = res['critiques'].iloc[0]
        self.assertEqual(crit['text_chars'], 38)
        self.assertTrue(pd.isna(crit['html']))
        self.assertEqual(res['origin_by_timezone'].iloc[0]['n_students'], 2)

    def test_participants_json_filters_deidentified_logs(self):
        deid = self.write_deidentified_logs()
        res = self.analyze(deid, participants=ParticipantFilter.load(deid / 'participants.json'))
        self.assertEqual(set(res['cases']['email']), {'aaaaaaaa'})
        self.assertEqual(set(res['usage_clean']['email']), {'aaaaaaaa'})

    def test_participants_and_exclusion_combine(self):
        res = self.analyze(participants=ParticipantFilter.load(self.packs), exclude_emails=[self.BOB])
        self.assertEqual(set(res['cases']['email']), {self.ALICE})

    def test_no_filter_prints_reminder(self):
        self.assertIn('REMINDER', self.analyze().report)

    def test_empty_participants_leaves_nothing_and_does_not_crash(self):
        res = self.analyze(participants=frozenset())
        self.assertTrue(res['cases'].empty)
        self.assertTrue(res['usage_clean'].empty)

    def test_origin_report(self):
        origin = self.analyze()['origin_by_timezone']
        self.assertEqual(list(origin.columns), OriginReport.COLUMNS)
        by_tz = origin.set_index('client_timezone')
        self.assertEqual(by_tz.loc['Europe/Berlin', 'tz_area'], 'Europe')
        self.assertEqual(by_tz.loc['America/Los_Angeles', 'n_students'], 1)
        self.assertEqual(int(by_tz['n_cases'].sum()), 3)

    def test_origin_report_without_cases(self):
        self.assertTrue(OriginReport.build(pd.DataFrame()).empty)

    def test_unclassifiable_line_is_reported_not_lost(self):
        with open(self.raw / 'content.jsonl', 'a') as fd:
            fd.write(json.dumps({self.key('x-2026-10-03T20-40-07-zzzzzz', self.ALICE): {'mystery': 1}}) + '\n')
        res = self.analyze()
        self.assertEqual(len(res.unrecognized), 1)

    def test_write_and_command_line(self):
        out = self.dir / 'results'
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            LogAnalyzer.main([str(self.raw), str(out), '--participants-file', str(self.packs)])
        names = {p.name for p in out.iterdir()}
        for expected in ('cases.csv', 'turns.csv', 'events.csv', 'critiques.csv', 'summary_by_user.csv',
                         'model_performance.csv', 'usage_clean.csv', 'action_counts.csv',
                         'origin_by_timezone.csv', 'report.txt'):
            self.assertIn(expected, names)
        self.assertIn('Participants only', buf.getvalue())
        cases = pd.read_csv(out / 'cases.csv')
        self.assertEqual(len(cases), 2)
        self.assertEqual(pd.read_csv(out / 'summary_by_user.csv').columns[0], 'email')


if __name__ == '__main__':
    unittest.main()
