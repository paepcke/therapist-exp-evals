# **********************************************************
#
# @Author: Andreas Paepcke
# @Date:   2026-10-10 19:03:52
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_analysis/log_utils.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:45:52
#
# **********************************************************

"""
log_utils.py

The parsing, joining and summarizing steps behind log_analysis.py,
as class methods of LogUtils, plus TextCounts for text-length fields.
See log_analysis.py for the log formats and for the command line.
"""

import csv
import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import pandas as pd
except ImportError:
    sys.exit("This tool requires pandas: pip install pandas --break-system-packages")


class LogUtils:
    """
    Stateless helpers for the usage and content logs. Everything is a
    class method; the class holds the constants they share.
    """

    # usage.csv timestamps are written in this zone (see UsageLogger):
    USAGE_LOG_TZ = 'US/Pacific'

    # A 'human' turn this close after a roleSwitch event is the front end
    # priming the LLM with the other party's last utterance, not something
    # the student typed:
    SEED_WINDOW_SECONDS = 2.0

    EXPERIMENT_PACK_PREFIX = 'EXPERIMENT_PACK_'

    # Rotation of the loggers' files: base.ext, base_1.ext, base_2.ext, ...
    ROTATION_SUFFIX_RE = re.compile(r'_(\d+)$')

    # Timestamp embedded by utils.js::mkCaseId (UTC), e.g.
    #   karenthompson-2025-08-26T08-01-19-jqj61g
    CASE_TIMESTAMP_RE = re.compile(
        r'(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})'
    )

    # Fields consumed by the typed columns of each record kind; whatever is
    # left over on an event record is preserved in 'extra_json':
    EVENT_KNOWN_FIELDS = {'event', 'fromRole', 'toRole', 'ok', 'content', 'datetimeUTC',
                          'contentChars', 'contentWords'}

    @classmethod
    def find_log_family(cls, directory, base_stem, suffix):
        """
        Return every file in `directory` belonging to the rotation family
        of `base_stem` + `suffix` (e.g. base_stem='content', suffix='.jsonl'
        matches content.jsonl, content_1.jsonl, content_2.jsonl, ...),
        sorted oldest-rotation-first.

        :param directory: directory to scan
        :param base_stem: file stem with no rotation suffix, e.g. 'content'
        :param suffix: file extension including the dot, e.g. '.jsonl'
        :return: list of Path, in rotation order
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise FileNotFoundError(f"Not a directory: {directory}")

        found = []
        for candidate in directory.glob(f"*{suffix}"):
            stem_root = cls.ROTATION_SUFFIX_RE.sub('', candidate.stem)
            if stem_root != base_stem:
                continue
            match = cls.ROTATION_SUFFIX_RE.search(candidate.stem)
            rotation_num = int(match.group(1)) if match else 0
            found.append((rotation_num, candidate))

        found.sort(key=lambda pair: pair[0])
        return [path for _, path in found]


    @classmethod
    def load_usage_logs(cls, files, dedupe_consecutive=True):
        """
        Load and concatenate a usage-log rotation family into one DataFrame,
        parsing timestamps and collapsing immediately-repeated identical
        rows if requested.

        model/runtime are populated only for COMPLETE_* rows; every other
        action has None for both. row.get() is used rather than row[...] so
        an older usage.csv lacking those header columns degrades to None.

        'date' is US/Pacific wall-clock time as written by UsageLogger;
        'date_utc' is the same instant as naive UTC, for joining against
        content.jsonl's datetimeUTC values. Wall-clock times that are
        ambiguous or nonexistent because of a DST change get NaT there.

        :param files: list of file paths, in rotation order
        :param dedupe_consecutive: whether to collapse back-to-back exact
            duplicate (date, email, action, model, runtime) rows
        :return: (DataFrame with columns [date, date_utc, email, action,
            model, runtime], number of duplicate rows collapsed)
        """
        rows = []
        for fpath in files:
            with open(fpath, newline='') as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    if not row.get('date') or not row.get('action'):
                        continue
                    rows.append((
                        row['date'],
                        row.get('email'),
                        row['action'],
                        row.get('model') or None,
                        row.get('runtime') or None,
                        ))

        dupes_collapsed = 0
        if dedupe_consecutive:
            deduped = []
            prev = None
            for row in rows:
                if row == prev:
                    dupes_collapsed += 1
                    continue
                deduped.append(row)
                prev = row
            rows = deduped

        df = pd.DataFrame(rows, columns=['date', 'email', 'action', 'model', 'runtime'])
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df['runtime'] = pd.to_numeric(df['runtime'], errors='coerce')
        try:
            df['date_utc'] = (df['date']
                              .dt.tz_localize(cls.USAGE_LOG_TZ, ambiguous='NaT', nonexistent='NaT')
                              .dt.tz_convert('UTC')
                              .dt.tz_localize(None))
        except Exception as e:   # e.g. no tz database installed: pip install tzdata
            print(f"WARNING: could not derive date_utc from usage log ({type(e).__name__}: {e})",
                  file=sys.stderr)
            df['date_utc'] = pd.NaT
        return df[['date', 'date_utc', 'email', 'action', 'model', 'runtime']], dupes_collapsed


    @classmethod
    def split_case_id(cls, raw_case_id):
        """
        Split a raw content_logger case_id into (case_key, email).

        The case_key portion (client name + timestamp + random suffix) is
        built without underscores, so the first underscore marks the start
        of the appended email. With no underscore, email is None.

        :param raw_case_id: the full jsonl top-level key
        :return: (case_key, email_or_None)
        """
        if '_' in raw_case_id:
            case_key, email = raw_case_id.split('_', 1)
        else:
            case_key, email = raw_case_id, None
        return case_key, email


    @classmethod
    def extract_case_timestamp(cls, case_key):
        """
        Pull the embedded creation timestamp (naive UTC) out of a case_key,
        restoring ':' in the time-of-day portion.

        :param case_key: case_key as returned by split_case_id()
        :return: pandas.Timestamp, or NaT if the pattern isn't found
        """
        match = cls.CASE_TIMESTAMP_RE.search(case_key)
        if not match:
            return pd.NaT
        date_part, time_part = match.group(1).split('T')
        time_part = time_part.replace('-', ':')
        return pd.to_datetime(f"{date_part}T{time_part}", errors='coerce')


    @classmethod
    def parse_utc(cls, value):
        """
        Parse an ISO-8601 'datetimeUTC' string into a naive-UTC Timestamp.

        :param value: string as produced by JS Date.toISOString(), or None
        :return: pandas.Timestamp (naive, UTC) or NaT
        """
        if not value:
            return pd.NaT
        try:
            ts = pd.to_datetime(value, utc=True, format='ISO8601')
        except (ValueError, TypeError):
            return pd.NaT
        return ts.tz_localize(None)


    @classmethod
    def html_to_text(cls, html_str):
        """
        Crude HTML-to-text for the critique pages: drop tags, unescape
        entities, collapse whitespace.

        :param html_str: HTML string
        :return: plain text
        """
        if not isinstance(html_str, str):
            return None
        text = re.sub(r'<[^>]+>', ' ', html_str)
        text = html.unescape(text)
        return re.sub(r'\s+', ' ', text).strip()


    @classmethod
    def load_content_logs(cls, files):
        """
        Load and concatenate a content-log rotation family into raw
        DataFrames, one per record kind. File order is preserved in a
        monotonically increasing 'seq' shared by all kinds.

        See the module docstring for the record kinds and the order in
        which they are recognized. Records are never silently dropped:
        whatever does not fit goes into the 'unrecognized' list.

        If a defenseGuess, studentInitialResponse or aiDiscussion record
        occurs more than once for a case, all are kept in their own table;
        enrich_cases() decides which one counts (the last).

        :param files: list of file paths, in rotation order
        :return: dict with keys 'cases', 'turns', 'events', 'guesses',
            'legacy' (DataFrames) and 'unrecognized' (list of dict)
        """
        case_rows = []
        turn_rows = []
        event_rows = []
        guess_rows = []
        legacy_rows = []
        unrecognized = []
        seq = 0

        for fpath in files:
            with open(fpath) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        raw_case_id, fields = next(iter(entry.items()))
                        if not isinstance(fields, dict):
                            raise ValueError("record value is not an object")
                    except (json.JSONDecodeError, StopIteration, ValueError, AttributeError) as e:
                        unrecognized.append({'file': str(fpath), 'line': line, 'error': str(e)})
                        continue

                    case_key, email = cls.split_case_id(raw_case_id)
                    seq += 1
                    base = {
                        'case_key': case_key,
                        'email': email,
                        'seq': seq,
                        'source_file': str(fpath),
                        }
                    dt = cls.parse_utc(fields.get('datetimeUTC'))

                    if 'event' in fields:
                        extra = {k: v for k, v in fields.items() if k not in cls.EVENT_KNOWN_FIELDS}
                        content = fields.get('content')
                        content_chars, content_words = TextCounts.of(fields, 'content', is_html=True)
                        event_rows.append({
                            **base,
                            'event': fields['event'],
                            'datetime_utc': dt,
                            'from_role': fields.get('fromRole'),
                            'to_role': fields.get('toRole'),
                            'ok': fields.get('ok'),
                            'content': content,
                            'extra_json': json.dumps(extra) if extra else None,
                            'content_chars': content_chars,
                            'content_words': content_words,
                            })
                    elif 'speaker' in fields:
                        # A missing text field means '' in raw logs; in de-identified
                        # logs the text is deliberately absent, which is None:
                        content = fields.get('content', None if TextCounts.has(fields, 'content') else '')
                        content_chars, content_words = TextCounts.of(fields, 'content')
                        turn_rows.append({
                            **base,
                            'datetime_utc': dt,
                            'student_role': fields.get('studentRole'),
                            'dialog_role': fields.get('dialogRole'),
                            'speaker': fields.get('speaker'),
                            'content': content,
                            'content_chars': content_chars,
                            'content_words': content_words,
                            })
                    elif TextCounts.has(fields, 'vignette'):
                        pt_says_counts = TextCounts.of(fields, 'ptSays')
                        ai_alt_counts = TextCounts.of(fields, 'aiAlt')
                        vignette_counts = TextCounts.of(fields, 'vignette')
                        case_rows.append({
                            **base,
                            'created_at': dt if pd.notna(dt) else cls.extract_case_timestamp(case_key),
                            'client_name': fields.get('name'),
                            'defense': fields.get('defense'),
                            'vignette': fields.get('vignette'),
                            'pt_says': fields.get('ptSays'),
                            'ai_alt': fields.get('aiAlt'),
                            'client_timezone': fields.get('clientTimezone'),
                            'vignette_chars': vignette_counts[0],
                            'vignette_words': vignette_counts[1],
                            'pt_says_chars': pt_says_counts[0],
                            'pt_says_words': pt_says_counts[1],
                            'ai_alt_chars': ai_alt_counts[0],
                            'ai_alt_words': ai_alt_counts[1],
                            })
                    elif 'defenseGuess' in fields:
                        guess_rows.append({
                            **base,
                            'datetime_utc': dt,
                            'defense_guess': fields['defenseGuess'],
                            })
                    elif TextCounts.has(fields, 'studentInitialResponse'):
                        chars, words = TextCounts.of(fields, 'studentInitialResponse')
                        legacy_rows.append({**base, 'field': 'student_initial_response',
                                            'value': fields.get('studentInitialResponse'),
                                            'value_chars': chars, 'value_words': words})
                    elif TextCounts.has(fields, 'aiDiscussion'):
                        chars, words = TextCounts.of(fields, 'aiDiscussion')
                        legacy_rows.append({**base, 'field': 'ai_discussion',
                                            'value': fields.get('aiDiscussion'),
                                            'value_chars': chars, 'value_words': words})
                    else:
                        unrecognized.append({'file': str(fpath), 'line': line,
                                             'error': 'unrecognized shape'})

        def frame(rows, cols):
            return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

        base_cols = ['case_key', 'email', 'seq', 'source_file']
        return {
            'cases': frame(case_rows, base_cols + ['created_at', 'client_name', 'defense',
                                                   'vignette', 'pt_says', 'ai_alt', 'client_timezone',
                                                   'vignette_chars', 'vignette_words',
                                                   'pt_says_chars', 'pt_says_words',
                                                   'ai_alt_chars', 'ai_alt_words']),
            'turns': frame(turn_rows, base_cols + ['datetime_utc', 'student_role', 'dialog_role',
                                                   'speaker', 'content', 'content_chars',
                                                   'content_words']),
            'events': frame(event_rows, base_cols + ['event', 'datetime_utc', 'from_role', 'to_role',
                                                     'ok', 'content', 'extra_json',
                                                     'content_chars', 'content_words']),
            'guesses': frame(guess_rows, base_cols + ['datetime_utc', 'defense_guess']),
            'legacy': frame(legacy_rows, base_cols + ['field', 'value', 'value_chars', 'value_words']),
            'unrecognized': unrecognized,
            }


    @classmethod
    def filter_out_emails(cls, df, emails_to_exclude):
        """
        Drop rows whose 'email' column matches one of emails_to_exclude.
        Rows with a missing/unknown email (None, NaN, or the literal
        string 'None' as written by usage_logger for pre-login actions)
        are always kept, since they can't be a test-account match.

        :param df: DataFrame with an 'email' column
        :param emails_to_exclude: iterable of email strings to drop
        :return: (filtered DataFrame, number of rows dropped)
        """
        if df.empty or not emails_to_exclude:
            return df, 0
        is_excluded = df['email'].isin(set(emails_to_exclude))
        dropped = int(is_excluded.sum())
        return df.loc[~is_excluded].reset_index(drop=True), dropped


    @classmethod
    def _norm(cls, s):
        """Normalize a defense label for comparison."""
        return s.strip().lower() if isinstance(s, str) else None


    @classmethod
    def build_pack_timeline(cls, usage_df):
        """
        Extract experiment-pack assignments from the usage log.

        :param usage_df: DataFrame from load_usage_logs()
        :return: DataFrame [email, date_utc, experiment_pack], sorted by
            date_utc, one row per assignment/reassignment
        """
        is_pack = usage_df['action'].str.startswith(cls.EXPERIMENT_PACK_PREFIX, na=False)
        packs = usage_df.loc[is_pack & usage_df['date_utc'].notna(), ['email', 'date_utc', 'action']].copy()
        packs['experiment_pack'] = packs['action'].str[len(cls.EXPERIMENT_PACK_PREFIX):]
        return packs[['email', 'date_utc', 'experiment_pack']].sort_values('date_utc').reset_index(drop=True)


    @classmethod
    def flag_seeded_turns(cls, turns_df, events_df):
        """
        Add 'is_role_switch_seed' to turns_df: True for human turns that
        arrive within SEED_WINDOW_SECONDS after a roleSwitch event in the
        same case (see module docstring).

        :return: turns_df (modified copy)
        """
        turns_df = turns_df.copy()
        turns_df['is_role_switch_seed'] = False
        if turns_df.empty or events_df.empty:
            return turns_df

        switches = events_df[events_df['event'] == 'roleSwitch']
        switch_times = {ck: grp['datetime_utc'].dropna().tolist()
                        for ck, grp in switches.groupby('case_key')}

        flags = []
        for case_key, dt, speaker in zip(turns_df['case_key'], turns_df['datetime_utc'], turns_df['speaker']):
            seeded = False
            if speaker == 'human' and pd.notna(dt):
                for sw in switch_times.get(case_key, ()):
                    gap = (dt - sw).total_seconds()
                    if 0 <= gap <= cls.SEED_WINDOW_SECONDS:
                        seeded = True
                        break
            flags.append(seeded)
        turns_df['is_role_switch_seed'] = flags
        return turns_df


    @classmethod
    def add_seconds_since_prev(cls, turns_df, cases_df, events_df, guesses_df):
        """
        Add 'secs_since_prev_record' to turns_df: seconds since the
        previous record (case-open, turn, event or guess) of the same
        case, in file order. For a human turn this is roughly the
        student's think-and-type time (plus any time spent reading a
        critique, if one came in between).

        :return: turns_df (modified copy)
        """
        turns_df = turns_df.copy()
        turns_df['secs_since_prev_record'] = pd.NA
        if turns_df.empty:
            return turns_df

        parts = [
            cases_df[['case_key', 'seq']].assign(dt=cases_df['created_at']),
            turns_df[['case_key', 'seq']].assign(dt=turns_df['datetime_utc']),
            events_df[['case_key', 'seq']].assign(dt=events_df['datetime_utc']),
            guesses_df[['case_key', 'seq']].assign(dt=guesses_df['datetime_utc']),
            ]
        timeline = pd.concat([p for p in parts if not p.empty], ignore_index=True)
        timeline['dt'] = pd.to_datetime(timeline['dt'])
        timeline = timeline.sort_values('seq')
        timeline['secs'] = timeline.groupby('case_key')['dt'].diff().dt.total_seconds()
        secs_by_seq = timeline.set_index('seq')['secs']
        turns_df['secs_since_prev_record'] = turns_df['seq'].map(secs_by_seq)
        return turns_df


    @classmethod
    def build_critiques(cls, events_df):
        """
        Pair each sessionCritiqueResult with the most recent unpaired
        sessionCritiqueRequested of the same case.

        :param events_df: events DataFrame
        :return: (critiques_df, unanswered_requests_by_case dict)
            critiques_df columns: case_key, email, seq, datetime_utc, ok,
            latency_s, html, text, text_chars
        """
        cols = ['case_key', 'email', 'seq', 'datetime_utc', 'ok', 'latency_s',
                'html', 'text', 'text_chars']
        rows = []
        unanswered = defaultdict(int)
        if events_df.empty:
            return pd.DataFrame(columns=cols), unanswered

        crit = events_df[events_df['event'].isin(['sessionCritiqueRequested', 'sessionCritiqueResult'])]
        for case_key, grp in crit.sort_values('seq').groupby('case_key'):
            pending = None
            for rec in grp.itertuples():
                if rec.event == 'sessionCritiqueRequested':
                    if pending is not None:
                        unanswered[case_key] += 1
                    pending = rec.datetime_utc
                else:
                    latency = None
                    if pending is not None and pd.notna(rec.datetime_utc) and pd.notna(pending):
                        latency = (rec.datetime_utc - pending).total_seconds()
                    pending = None
                    text = cls.html_to_text(rec.content)
                    if isinstance(rec.content, str):
                        text_chars = len(text) if text else None
                    else:
                        # De-identified log: the HTML is gone, only its plain-text length remains
                        text_chars = rec.content_chars if pd.notna(rec.content_chars) else None
                    rows.append({
                        'case_key': case_key,
                        'email': rec.email,
                        'seq': rec.seq,
                        'datetime_utc': rec.datetime_utc,
                        'ok': bool(rec.ok) if pd.notna(rec.ok) else None,
                        'latency_s': latency,
                        'html': rec.content,
                        'text': text,
                        'text_chars': text_chars,
                        })
            if pending is not None:
                unanswered[case_key] += 1
        return pd.DataFrame(rows, columns=cols), unanswered


    @classmethod
    def local_time(cls, ts, tz_name):
        """
        Convert a naive-UTC Timestamp to naive local wall-clock time in the
        IANA zone tz_name. NaT if either is unusable.
        """
        if pd.isna(ts) or not isinstance(tz_name, str):
            return pd.NaT
        try:
            return ts.tz_localize('UTC').tz_convert(ZoneInfo(tz_name)).tz_localize(None)
        except Exception:
            return pd.NaT


    @classmethod
    def enrich_cases(cls, cases_df, turns_df, events_df, guesses_df, legacy_df, critiques_df,
                     unanswered_crit, pack_timeline):
        """
        Build the final cases table: case-open fields plus everything the
        other record kinds say about the case.

        Added columns: created_at_local, local_hour, local_weekday,
        experiment_pack, defense_guess, defense_guess_at, secs_to_guess,
        guess_correct (case-insensitive exact match of guess to defense;
        note guesses are free choices from a menu whose labels may not
        coincide with the generator's defense vocabulary), n_guesses,
        n_turns, n_student_turns (human, not role-switch seeds),
        n_ai_turns, n_questioning_turns, n_role_switches,
        n_critique_requests, n_critique_ok, n_critique_failed,
        first_record_at, last_record_at, duration_min, plus legacy
        student_initial_response / ai_discussion (last value wins).

        :return: DataFrame, one row per case-open record
        """
        if cases_df.empty:
            return cases_df.copy()

        df = cases_df.copy()
        df['created_at'] = pd.to_datetime(df['created_at'])

        # Local time
        df['created_at_local'] = [cls.local_time(ts, tz) for ts, tz in zip(df['created_at'], df['client_timezone'])]
        df['created_at_local'] = pd.to_datetime(df['created_at_local'])
        df['local_hour'] = df['created_at_local'].dt.hour
        df['local_weekday'] = df['created_at_local'].dt.day_name()

        # Experiment pack in force when the case was opened
        df['experiment_pack'] = pd.NA
        if not pack_timeline.empty:
            left = df.loc[df['created_at'].notna(), ['seq', 'email', 'created_at']].sort_values('created_at')
            merged = pd.merge_asof(left, pack_timeline, left_on='created_at', right_on='date_utc',
                                   by='email', direction='backward')
            df['experiment_pack'] = df['seq'].map(merged.set_index('seq')['experiment_pack'])

        # Defense guess (last one wins; n_guesses counts them all)
        if guesses_df.empty:
            df['defense_guess'] = pd.NA
            df['defense_guess_at'] = pd.NaT
            df['n_guesses'] = 0
        else:
            last = guesses_df.sort_values('seq').groupby('case_key').tail(1).set_index('case_key')
            df['defense_guess'] = df['case_key'].map(last['defense_guess'])
            df['defense_guess_at'] = pd.to_datetime(df['case_key'].map(last['datetime_utc']))
            df['n_guesses'] = df['case_key'].map(guesses_df.groupby('case_key').size()).fillna(0).astype(int)
        df['secs_to_guess'] = (df['defense_guess_at'] - df['created_at']).dt.total_seconds()
        df['guess_correct'] = [
            (cls._norm(g) == cls._norm(d)) if (isinstance(g, str) and isinstance(d, str)) else pd.NA
            for g, d in zip(df['defense_guess'], df['defense'])
            ]

        # Turn counts
        def count_by_case(frame, mask=None):
            if frame.empty:
                return pd.Series(dtype=int)
            sel = frame if mask is None else frame[mask]
            return sel.groupby('case_key').size()

        human = turns_df['speaker'] == 'human' if not turns_df.empty else None
        seeded = turns_df['is_role_switch_seed'] if not turns_df.empty else None
        count_specs = {
            'n_turns': count_by_case(turns_df),
            'n_student_turns': count_by_case(turns_df, (human & ~seeded) if not turns_df.empty else None),
            'n_ai_turns': count_by_case(turns_df, (turns_df['speaker'] == 'AI') if not turns_df.empty else None),
            'n_questioning_turns': count_by_case(
                turns_df, (turns_df['student_role'] == 'questioning') if not turns_df.empty else None),
            'n_role_switches': count_by_case(
                events_df, (events_df['event'] == 'roleSwitch') if not events_df.empty else None),
            'n_critique_requests': count_by_case(
                events_df, (events_df['event'] == 'sessionCritiqueRequested') if not events_df.empty else None),
            'n_critique_ok': count_by_case(
                critiques_df, (critiques_df['ok'] == True) if not critiques_df.empty else None),  # noqa: E712
            'n_critique_failed': count_by_case(
                critiques_df, (critiques_df['ok'] == False) if not critiques_df.empty else None),  # noqa: E712
            }
        for col, series in count_specs.items():
            df[col] = df['case_key'].map(series).fillna(0).astype(int)
        df['n_critique_unanswered'] = df['case_key'].map(unanswered_crit).fillna(0).astype(int)

        # Duration: first to last record of any kind in the case
        stamps = pd.concat([
            df[['case_key']].assign(dt=df['created_at']),
            turns_df[['case_key']].assign(dt=turns_df['datetime_utc']) if not turns_df.empty else None,
            events_df[['case_key']].assign(dt=events_df['datetime_utc']) if not events_df.empty else None,
            guesses_df[['case_key']].assign(dt=guesses_df['datetime_utc']) if not guesses_df.empty else None,
            ], ignore_index=True)
        stamps['dt'] = pd.to_datetime(stamps['dt'])
        span = stamps.groupby('case_key')['dt'].agg(['min', 'max'])
        df['first_record_at'] = df['case_key'].map(span['min'])
        df['last_record_at'] = df['case_key'].map(span['max'])
        df['duration_min'] = ((df['last_record_at'] - df['first_record_at']).dt.total_seconds() / 60).round(2)

        # Legacy fields, last value wins; always create the columns
        for field in ('student_initial_response', 'ai_discussion'):
            if legacy_df.empty:
                df[field] = pd.NA
                df[field + '_chars'] = pd.NA
                df[field + '_words'] = pd.NA
            else:
                sub = legacy_df[legacy_df['field'] == field].sort_values('seq').groupby('case_key').tail(1)
                sub = sub.set_index('case_key')
                df[field] = df['case_key'].map(sub['value'])
                df[field + '_chars'] = df['case_key'].map(sub['value_chars'])
                df[field + '_words'] = df['case_key'].map(sub['value_words'])

        return df


    @classmethod
    def build_user_summary(cls, usage_df, cases_df, turns_df, events_df, guesses_df):
        """
        Build one row per email joining usage-log activity with
        content-log case/turn/event activity.

        usage_actions_total counts every row; usage_actions_excl_log_content
        drops LOG_CONTENT rows, which are bookkeeping, one per content
        record. usage_log_content_rows is kept next to content_records_total
        as a consistency check: the two should roughly agree for users whose
        logs are entirely inside the analyzed window.

        :return: DataFrame indexed by email
        """
        summary = defaultdict(dict)
        no_email = '(no email found)'

        usage_known = usage_df[usage_df['email'].notna() & (usage_df['email'] != 'None')]
        for email, grp in usage_known.groupby('email'):
            s = summary[email]
            s['usage_actions_total'] = len(grp)
            s['usage_actions_excl_log_content'] = int((grp['action'] != 'LOG_CONTENT').sum())
            s['usage_log_content_rows'] = int((grp['action'] == 'LOG_CONTENT').sum())
            s['usage_logins'] = int((grp['action'] == 'LOGIN').sum())
            s['usage_completions'] = int(grp['action'].str.startswith('COMPLETE', na=False).sum())
            s['usage_new_cases'] = int((grp['action'] == 'COMPLETE_NEW_CASE').sum())
            s['usage_session_critiques'] = int((grp['action'] == 'COMPLETE_SESSION_CRITIQUE').sum())
            s['usage_first_seen'] = grp['date'].min()
            s['usage_last_seen'] = grp['date'].max()
            s['usage_active_days'] = grp['date'].dt.date.nunique()
            packs = grp.loc[grp['action'].str.startswith(cls.EXPERIMENT_PACK_PREFIX, na=False), 'action']
            if len(packs):
                s['experiment_pack_latest'] = packs.iloc[-1][len(cls.EXPERIMENT_PACK_PREFIX):]
            models = grp['model'].dropna().unique()
            if len(models):
                s['models_used'] = ','.join(sorted(models))

        if not cases_df.empty:
            for email, grp in cases_df.groupby('email', dropna=False):
                s = summary[email if email is not None else no_email]
                s['content_cases'] = len(grp)
                s['content_defenses_practiced'] = grp['defense'].nunique()
                s['content_role_switches'] = int(grp['n_role_switches'].sum())
                s['content_critique_requests'] = int(grp['n_critique_requests'].sum())
                s['content_critiques_ok'] = int(grp['n_critique_ok'].sum())
                s['content_critiques_failed'] = int(grp['n_critique_failed'].sum())
                guessed = grp['guess_correct'].dropna()
                s['content_defense_guesses'] = len(guessed)
                s['content_defense_guess_accuracy'] = (
                    round(float(guessed.astype(bool).mean()), 2) if len(guessed) else None)
                s['content_median_case_duration_min'] = grp['duration_min'].median()
                tzs = grp['client_timezone'].dropna().unique()
                if len(tzs):
                    s['client_timezones'] = ','.join(sorted(tzs))

        if not turns_df.empty:
            for email, grp in turns_df.groupby('email', dropna=False):
                s = summary[email if email is not None else no_email]
                student = grp[(grp['speaker'] == 'human') & ~grp['is_role_switch_seed']]
                s['content_turns_total'] = len(grp)
                s['content_turns_student'] = len(student)
                s['content_turns_ai'] = int((grp['speaker'] == 'AI').sum())
                s['content_turns_role_switch_seed'] = int(grp['is_role_switch_seed'].sum())
                s['content_avg_student_words_per_turn'] = (
                    round(student['content_words'].mean(), 1) if len(student) else None)

        if not events_df.empty:
            for email, grp in events_df.groupby('email', dropna=False):
                summary[email if email is not None else no_email]['content_event_records'] = len(grp)

        # Total content records per user, to line up against LOG_CONTENT rows:
        totals = defaultdict(int)
        for frame in (cases_df, turns_df, events_df, guesses_df):
            if not frame.empty:
                for email, n in frame.groupby('email', dropna=False).size().items():
                    totals[email if email is not None else no_email] += int(n)
        for email, n in totals.items():
            summary[email]['content_records_total'] = n   # case-open + turn + event + defense-guess records

        df = pd.DataFrame.from_dict(summary, orient='index')
        df.index.name = 'email'
        return df.sort_index()


    @classmethod
    def build_model_performance_summary(cls, usage_df):
        """
        Summarize completion latency per (model, flow), from the usage
        log's model/runtime columns. The flow is the action label
        (COMPLETE_NEW_CASE, COMPLETE_DISCUSSION, COMPLETE_SESSION_CRITIQUE,
        ...; plain COMPLETE for requests without a purpose).

        Rows without both model and runtime are excluded.

        :param usage_df: DataFrame as returned by load_usage_logs()
        :return: DataFrame indexed by (model, action), columns
            [count, mean, std, min, max] (seconds), sorted by mean
            runtime ascending. Empty (but correctly shaped) if no rows
            have both a model and a runtime.
        """
        completions = usage_df[usage_df['runtime'].notna() & usage_df['model'].notna()]

        if completions.empty:
            empty_index = pd.MultiIndex.from_arrays([[], []], names=['model', 'action'])
            return pd.DataFrame(columns=['count', 'mean', 'std', 'min', 'max'], index=empty_index)

        summary = completions.groupby(['model', 'action'])['runtime'].agg(
            count='count', mean='mean', std='std', min='min', max='max')
        return summary.round(2).sort_values('mean')


class TextCounts:
    """
    Character and word counts of a record's text field, from either
    the text itself (raw logs) or the '<field>Chars' / '<field>Words'
    counts that log_deid leaves in its place (de-identified logs).
    """

    CHARS_SUFFIX = 'Chars'
    WORDS_SUFFIX = 'Words'

    @classmethod
    def of(cls, fields, name, is_html=False):
        """
        :param fields: the record's field dict
        :param name: text field name, e.g. 'content'
        :param is_html: whether the text is HTML; counts are then of
            the plain text, as log_deid measures it
        :return: (chars, words); (None, None) if the record has neither
            the text nor counts for it
        """
        value = fields.get(name)
        if isinstance(value, str):
            if is_html:
                value = LogUtils.html_to_text(value)
            return len(value), len(value.split())
        return (cls._count(fields.get(name + cls.CHARS_SUFFIX)),
                cls._count(fields.get(name + cls.WORDS_SUFFIX)))

    @classmethod
    def has(cls, fields, name):
        """
        :return: True if the record carries the text field or its counts
        """
        return name in fields or (name + cls.CHARS_SUFFIX) in fields

    @staticmethod
    def _count(value):
        """Counts must be ints; anything else is treated as absent."""
        return value if isinstance(value, int) and not isinstance(value, bool) else None
