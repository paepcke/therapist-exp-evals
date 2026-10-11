# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 09:49:10
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_dashboard/dashboard_data.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:49:10
# **********************************************************

'''
dashboard_data.py

Turns the de-identified logs into the one JSON document the dashboard
page draws. The analysis is log_analysis's; this adds the aggregates
the page shows, and a cache that is renewed when a log file changes.
'''

import math
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from log_analysis.log_analysis import LogAnalyzer, ParticipantFilter
from log_analysis.log_utils import LogUtils


class ParticipantsUnavailable(RuntimeError):
    '''
    Raised when only participants are requested, but the cloud machine has
    no participants.json yet.
    '''


class IdentifyingDataError(RuntimeError):
    '''
    Raised when the logs hold email addresses, which means they were
    not de-identified. Nothing is shown then.
    '''


class DashboardData:
    '''
    Computes and caches the dashboard document.
    '''

    UNASSIGNED_PACK = '(none)'

    # Word-count bins for student turns: [lo, next lo); the last is open-ended
    HISTOGRAM_EDGES = (0, 5, 10, 20, 40, 80, 160)

    # ------------------------- Constructor ---------------------

    def __init__(self, config):
        '''
        :param config: settings
        :type config: DashboardConfig
        '''
        self._config = config
        self._lock = threading.Lock()
        self._cache = {}        # participants_only -> (signature, document)

    # ------------------------- Public API ----------------------

    @property
    def participants_available(self):
        '''
        :return: whether participants.json has arrived
        :rtype: bool
        '''
        return self._config.participants_path.is_file()

    def document(self, participants_only):
        '''
        :param participants_only: restrict to the listed participants
        :type participants_only: bool
        :return: JSON-ready dict; the cached one if no log file changed
        :rtype: dict
        :raises ParticipantsUnavailable: if participants_only but no file
        :raises IdentifyingDataError: if the logs contain email addresses
        '''
        with self._lock:
            signature = self._signature()
            cached = self._cache.get(bool(participants_only))
            if cached and cached[0] == signature:
                return cached[1]
            document = self._build(bool(participants_only))
            self._cache[bool(participants_only)] = (signature, document)
            return document

    # ------------------------- Building ------------------------

    def _signature(self):
        '''
        :return: what the files looked like: (name, size, mtime) of each
        :rtype: tuple
        '''
        files = (LogUtils.find_log_family(self._config.log_dir, LogAnalyzer.USAGE_STEM, LogAnalyzer.USAGE_SUFFIX)
                 + LogUtils.find_log_family(self._config.log_dir, LogAnalyzer.CONTENT_STEM, LogAnalyzer.CONTENT_SUFFIX))
        if self.participants_available:
            files.append(self._config.participants_path)
        return tuple((f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files)

    def _build(self, participants_only):
        participants = None
        if participants_only:
            if not self.participants_available:
                raise ParticipantsUnavailable("participants.json has not arrived yet")
            participants = ParticipantFilter.load(self._config.participants_path)

        result = LogAnalyzer(self._config.log_dir, participants=participants).analyze()
        self._refuse_identifying_data(result)

        cases = result['cases']
        practice_n = self._config.trial_case_count
        progress = self._progress(cases, result['summary_by_user'], participants, practice_n)
        graded = cases['guess_correct'].dropna() if not cases.empty else pd.Series(dtype=object)
        document = {
            'meta': {
                'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'data_through': self._data_through(result),
                'participants_only': participants_only,
                'participants_available': self.participants_available,
                'participants_listed': len(participants) if participants is not None else None,
                'practice_cases': practice_n,
                'files': [{'name': n, 'bytes': s} for n, s, _ in self._signature()],
                'notes': [ln for ln in result.report_lines if ln.startswith(('WARNING', 'NOTE'))],
                'unrecognized_lines': len(result.unrecognized),
                },
            'kpis': {
                'participants_started': int((pd.Series([p['cases'] for p in progress]) > 0).sum()) if progress else 0,
                'cases': len(cases),
                'real_cases': sum(p['real_cases'] for p in progress),
                'student_turns': int(cases['n_student_turns'].sum()) if not cases.empty else 0,
                'guesses': int(len(graded)),
                'guess_accuracy': (float(graded.astype(bool).mean()) if len(graded) else None),
                'critiques_ok': int(cases['n_critique_ok'].sum()) if not cases.empty else 0,
                'critiques_failed': int(cases['n_critique_failed'].sum()) if not cases.empty else 0,
                'critiques_unanswered': int(cases['n_critique_unanswered'].sum()) if not cases.empty else 0,
                },
            'packs': self._pack_names(cases),
            'cases_by_day': self._cases_by_day(cases),
            'progress': progress,
            'turn_length': self._turn_length(result['turns']),
            'origin': result['origin_by_timezone'].rename(columns={'client_timezone': 'tz', 'tz_area': 'area',
                                                                   'n_students': 'students',
                                                                   'n_cases': 'cases'}).to_dict('records'),
            'guess_by_defense': self._guess_by_defense(cases),
            'models': result['model_performance'].reset_index().to_dict('records'),
            }
        return self._jsonable(document)

    @staticmethod
    def _refuse_identifying_data(result):
        for name in ('usage_clean', 'cases', 'turns', 'events'):
            frame = result[name]
            if not frame.empty and frame['email'].astype(str).str.contains('@', regex=False).any():
                raise IdentifyingDataError(
                    f"The logs contain email addresses (in {name}); they were not de-identified. "
                    f"Nothing is shown.")

    @staticmethod
    def _data_through(result):
        stamps = []
        for name, column in (('usage_clean', 'date_utc'), ('turns', 'datetime_utc'), ('cases', 'last_record_at')):
            frame = result[name]
            if not frame.empty and frame[column].notna().any():
                stamps.append(pd.to_datetime(frame[column]).max())
        return max(stamps) if stamps else None

    @classmethod
    def _pack_names(cls, cases):
        if cases.empty:
            return []
        names = sorted(str(p) for p in cases['experiment_pack'].dropna().unique())
        return names + ([cls.UNASSIGNED_PACK] if cases['experiment_pack'].isna().any() else [])

    @classmethod
    def _cases_by_day(cls, cases):
        if cases.empty:
            return []
        frame = pd.DataFrame({
            'day': pd.to_datetime(cases['created_at']).dt.strftime('%Y-%m-%d'),
            'pack': cases['experiment_pack'].map(lambda p: cls.UNASSIGNED_PACK if pd.isna(p) else str(p)),
            }).dropna(subset=['day'])
        return (frame.groupby(['day', 'pack']).size().rename('n').reset_index()
                .sort_values(['day', 'pack']).to_dict('records'))

    @classmethod
    def _progress(cls, cases, summary, participants, practice_n):
        '''
        One row per participant: cases opened, how many of them past the
        practice cases, when last active. With a participant list, those who
        have not started appear too, with zero.
        '''
        rows = {}
        if not cases.empty:
            for identity, grp in cases.groupby('email', dropna=True):
                key = ParticipantFilter.normalize(identity) or str(identity)
                packs = grp.sort_values('created_at')['experiment_pack'].dropna()
                n = len(grp)
                rows[key] = {
                    'id': key,
                    'pack': str(packs.iloc[-1]) if len(packs) else cls.UNASSIGNED_PACK,
                    'cases': n,
                    'real_cases': max(0, n - practice_n),
                    'active_days': int(pd.to_datetime(grp['created_at']).dt.date.nunique()),
                    'last_active': pd.to_datetime(grp['last_record_at']).max(),
                    }
        for identity in sorted(participants or ()):
            rows.setdefault(identity, {'id': identity, 'pack': cls.UNASSIGNED_PACK, 'cases': 0,
                                       'real_cases': 0, 'active_days': 0, 'last_active': None})
        if not summary.empty and 'experiment_pack_latest' in summary.columns:
            for identity, pack in summary['experiment_pack_latest'].dropna().items():
                key = ParticipantFilter.normalize(identity) or str(identity)
                if key in rows and rows[key]['pack'] == cls.UNASSIGNED_PACK:
                    rows[key]['pack'] = str(pack)
        return sorted(rows.values(), key=lambda r: (-r['real_cases'], -r['cases'], r['id']))

    @classmethod
    def _turn_length(cls, turns):
        edges = cls.HISTOGRAM_EDGES
        labels = [f"{lo}–{hi - 1}" for lo, hi in zip(edges, edges[1:])] + [f"{edges[-1]}+"]
        counts = [0] * len(labels)
        if not turns.empty:
            student = turns[(turns['speaker'] == 'human') & ~turns['is_role_switch_seed']]
            for words in student['content_words'].dropna():
                counts[int(np.searchsorted(edges, words, side='right')) - 1] += 1
        lows = list(edges)
        return [{'label': lab, 'lo': lo, 'n': n} for lab, lo, n in zip(labels, lows, counts)]

    @staticmethod
    def _guess_by_defense(cases):
        if cases.empty:
            return []
        graded = cases.dropna(subset=['guess_correct', 'defense'])
        if graded.empty:
            return []
        grouped = graded.groupby('defense')['guess_correct'].agg(
            n='count', correct=lambda s: int(s.astype(bool).sum())).reset_index()
        return grouped.sort_values(['n', 'defense'], ascending=[False, True]).to_dict('records')

    # ------------------------- JSON ----------------------------

    @classmethod
    def _jsonable(cls, value):
        '''
        :return: value with numpy numbers as Python numbers, timestamps
            as ISO strings, and NaN/NA as None
        '''
        if isinstance(value, dict):
            return {str(k): cls._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(v) for v in value]
        if value is None or value is pd.NaT or value is pd.NA:
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, (np.bool_, bool)):
            return bool(value)
        if isinstance(value, (np.integer, int)):
            return int(value)
        if isinstance(value, (np.floating, float)):
            return None if math.isnan(value) or math.isinf(value) else float(value)
        return value if isinstance(value, str) else str(value)
