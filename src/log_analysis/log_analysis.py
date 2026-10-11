#!/usr/bin/env python3
# **********************************************************
#
# @Author: Andreas Paepcke
# @Date:   2026-08-27 15:03:11
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_analysis/log_analysis.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:45:52
#
# **********************************************************

"""
log_analysis.py

Extracts and joins thtrainer's usage_logger (CSV) and content_logger
(JSONL) output into tidy tables for research analysis.

Log formats understood (as of Oct 2026)
---------------------------------------

usage.csv:   date,email,action,model,runtime
    * 'date' is server-local US/Pacific wall-clock time, no zone marker.
      A UTC version ('date_utc') is derived so usage rows can be lined up
      with content.jsonl, whose timestamps are all UTC.
    * Actions seen in production: LOGIN (email is the literal string
      'None'), EXPERIMENT_PACK_<NAME> (once per participant on first
      assignment or reassignment), SET_PRELUDE, ADD_PRELUDE, LOG_CONTENT
      (one row per content.jsonl record), and COMPLETE_<PURPOSE>
      (NEW_CASE, DISCUSSION, SESSION_CRITIQUE, ...) which are the only
      rows carrying model and runtime. A failed completion logs no row.

content.jsonl:   one record per line, {"<case_id>": {fields...}}, where
    case_id = <clientname>-<utc-iso-with-hyphens>-<rand6>_<email>.
    Record kinds, told apart by which key they carry (checked in this
    order):
        'event'           -> event record: roleSwitch (fromRole, toRole),
                             sessionCritiqueRequested,
                             sessionCritiqueResult (ok, content=HTML).
                             Unknown future event names are kept
                             (with any extra fields as JSON) rather than
                             dropped.
        'speaker'         -> dialogue turn (studentRole, dialogRole,
                             speaker, content, datetimeUTC)
        'vignette'        -> case-open (name, vignette, defense, ptSays,
                             aiAlt, datetimeUTC, clientTimezone)
        'defenseGuess'    -> the student's guess of the client's defense
        'studentInitialResponse', 'aiDiscussion'
                          -> LEGACY (features since removed); still folded
                             into cases.csv for old logs.
    Anything else goes to unrecognized.jsonl.

De-identified logs (log_deid package) are read as well. There the
email in usage.csv and in the content keys is the participant's
pseudonym code, and no free text is present: each text field
('content', 'vignette', 'ptSays', 'aiAlt', and the legacy fields) is
replaced by '<field>Chars' and '<field>Words'. The record kind is then
recognized by the '<field>Chars' marker (e.g. 'vignetteChars'), and the
character/word columns are filled from the counts. In the tables
the 'email' column then holds the code, and text columns ('content',
'vignette', 'html', ...) are empty.

Handles, deliberately:

  * Log rotation: both loggers roll over to base_1.ext, base_2.ext, ...
    This tool finds and concatenates a whole rotation family, in
    rotation order.

  * The email embedded in content_logger case_ids: the client-name/
    timestamp/random part never contains an underscore, so splitting the
    raw key on the FIRST underscore separates case_key from email.

  * Historical duplicate usage-log rows (a since-fixed FileHandler leak):
    --dedupe-usage collapses immediately-repeated identical rows.

  * Role-switch "seeding": when the student switches role, the front end
    immediately re-posts the other party's last utterance as a 'human'
    turn so the LLM can take over. Those turns (human turns within
    SEED_WINDOW_SECONDS of a roleSwitch event) are flagged
    is_role_switch_seed=True and excluded from the student-authored
    word counts.

Usage:
    python log_analysis.py /path/to/logs ./out
    python log_analysis.py /path/to/logs ./out --dedupe-usage
    python log_analysis.py /path/to/logs ./out --filter-email me@stanford.edu --filter-email test@x.com
    python log_analysis.py /path/to/logs ./out --participants-file /path/to/experiment_packs.json
    python log_analysis.py /path/to/deid-logs ./out --participants-file /path/to/deid-logs/participants.json

--participants-file restricts all results to the people listed under
'participants' in the given experiment_packs.json (emails), or in the
participants.json that log_deid writes (pseudonym codes). Rows without
an identity (e.g. LOGIN rows, whose email is 'None') cannot be shown to
belong to a participant and are dropped by this filter.

Both usage.csv (+ rotations) and content.jsonl (+ rotations) are
expected to live in the same logdir, with those hardwired base names.

Outputs (written to outdir):
    usage_clean.csv     - parsed usage log, plus date_utc
    action_counts.csv   - usage rows per action
    cases.csv           - one row per case: vignette, defense, defense guess
                          and correctness, experiment pack, local time,
                          turn/role-switch/critique counts, duration
    turns.csv           - one row per dialogue turn, with timestamps,
                          dialog role, seconds since previous record
    events.csv          - one row per event record
    critiques.csv       - one row per Session Critique result: ok flag,
                          latency from request, HTML and plain text
    summary_by_user.csv - per-email rollup joining both logs
    model_performance.csv - runtime stats per (model, action)
    origin_by_timezone.csv - students and cases per session timezone
                          (the proxy for where students come from)
    report.txt         - human-readable summary printed to stdout too
    unrecognized.jsonl  - lines that could not be classified (if any)
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("This tool requires pandas: pip install pandas --break-system-packages")

try:
    from log_analysis.log_utils import LogUtils
except ImportError:    # run as a script from this directory
    from log_utils import LogUtils


# ------------------------------------------------------------------
# Participant filtering and student origin
# ------------------------------------------------------------------

class ParticipantFilter:
    """
    Inclusion filter: keep only the people listed under 'participants'
    in experiment_packs.json (keys are emails), or in the participants.json
    that log_deid writes (keys are pseudonym codes). Which of the two it
    is does not matter here: the keys are compared with the 'email'
    column of whatever logs are being analyzed.
    """

    PARTICIPANTS_KEY = 'participants'

    # Values of the 'email' column that identify nobody:
    NO_IDENTITY = frozenset({'', 'none', 'nan'})

    @classmethod
    def normalize(cls, identity):
        """
        :param identity: an email, a pseudonym code, or a missing value
        :return: lower-cased, stripped identity; None if it identifies nobody
        """
        if not isinstance(identity, str):
            return None
        identity = identity.strip().lower()
        return None if identity in cls.NO_IDENTITY else identity

    @classmethod
    def load(cls, path):
        """
        :param path: experiment_packs.json or participants.json
        :return: frozenset of normalized participant identities
        :raises ValueError: if the file has no usable 'participants' entry
        """
        with open(path, 'r', encoding='utf-8') as fd:
            config = json.load(fd)
        participants = config.get(cls.PARTICIPANTS_KEY) if isinstance(config, dict) else None
        if isinstance(participants, dict):
            identities = participants.keys()
        elif isinstance(participants, list):
            identities = participants
        else:
            raise ValueError(f"{path} has no '{cls.PARTICIPANTS_KEY}' object or list")
        return frozenset(i for i in map(cls.normalize, identities) if i is not None)

    @classmethod
    def apply(cls, df, participants):
        """
        Keep only rows whose 'email' is a participant. Rows without an
        identity are dropped, since they cannot be shown to belong to one.

        :param df: DataFrame with an 'email' column
        :param participants: identities from load()
        :return: (filtered DataFrame, number of rows dropped)
        """
        if df.empty:
            return df, 0
        keep = df['email'].map(cls.normalize).isin(participants)
        return df.loc[keep].reset_index(drop=True), int((~keep).sum())


class OriginReport:
    """
    Where students come from, as far as the logs say: the IANA timezone
    of their browser sessions (case-open records' clientTimezone).
    """

    COLUMNS = ['client_timezone', 'tz_area', 'n_students', 'n_cases']

    @classmethod
    def build(cls, cases_df):
        """
        :param cases_df: enriched cases table
        :return: DataFrame [client_timezone, tz_area, n_students, n_cases],
            most cases first. tz_area is the zone's first path component
            ('America', 'Europe', ...). n_students counts distinct 'email'
            values (pseudonym codes in de-identified logs); a student who
            traveled counts once per zone.
        """
        if cases_df.empty or cases_df['client_timezone'].dropna().empty:
            return pd.DataFrame(columns=cls.COLUMNS)
        grouped = cases_df.dropna(subset=['client_timezone']).groupby('client_timezone')
        out = pd.DataFrame({
            'n_students': grouped['email'].nunique(),
            'n_cases': grouped.size(),
            }).reset_index()
        out['tz_area'] = out['client_timezone'].str.split('/').str[0]
        return out[cls.COLUMNS].sort_values(['n_cases', 'client_timezone'],
                                            ascending=[False, True]).reset_index(drop=True)


# ------------------------------------------------------------------
# Analysis driver
# ------------------------------------------------------------------

@dataclass
class AnalysisResult:
    """
    Everything one analysis run produced.

    :param tables: file stem -> DataFrame, as written to the output directory
        (usage_clean, action_counts, cases, turns, events, critiques,
        summary_by_user, model_performance, origin_by_timezone)
    :param unrecognized: content log lines that could not be classified
    :param report_lines: the human-readable report
    """
    tables: dict = field(default_factory=dict)
    unrecognized: list = field(default_factory=list)
    report_lines: list = field(default_factory=list)

    # Tables whose index is meaningful and must be written:
    INDEXED = ('summary_by_user', 'model_performance')

    def __getitem__(self, name):
        return self.tables[name]

    @property
    def report(self):
        return '\n'.join(self.report_lines) + '\n'

    def write(self, out_dir):
        """
        Write every table as <name>.csv, plus report.txt, and
        unrecognized.jsonl if any lines were not classified.

        :param out_dir: directory, created if needed
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, df in self.tables.items():
            df.to_csv(out_dir / f'{name}.csv', index=name in self.INDEXED)
        if self.unrecognized:
            with open(out_dir / 'unrecognized.jsonl', 'w') as fh:
                for item in self.unrecognized:
                    fh.write(json.dumps(item) + '\n')
        with open(out_dir / 'report.txt', 'w') as fh:
            fh.write(self.report)


class LogAnalyzer:
    """
    Runs the whole analysis over one log directory and returns the
    tables, for the command line and for the dashboard service alike.
    """

    USAGE_STEM = 'usage'
    USAGE_SUFFIX = '.csv'
    CONTENT_STEM = 'content'
    CONTENT_SUFFIX = '.jsonl'

    def __init__(self, logdir, dedupe_usage=False, exclude_emails=(), participants=None):
        """
        :param logdir: directory with both log families (raw or de-identified)
        :param dedupe_usage: collapse consecutive duplicate usage rows
        :param exclude_emails: identities to drop from all results
        :param participants: if not None, identities (from
            ParticipantFilter.load) to which all results are restricted
        """
        self.logdir = logdir
        self.dedupe_usage = dedupe_usage
        self.exclude_emails = list(exclude_emails)
        self.participants = None if participants is None else frozenset(participants)

    # ------------------------- Public API ----------------------

    def analyze(self):
        """
        :return: the tables and the report
        :rtype: AnalysisResult
        """
        lines = []
        report = lines.append
        report_blank = lambda: report("")   # noqa: E731

        usage_files = LogUtils.find_log_family(self.logdir, self.USAGE_STEM, self.USAGE_SUFFIX)
        content_files = LogUtils.find_log_family(self.logdir, self.CONTENT_STEM, self.CONTENT_SUFFIX)

        if not usage_files:
            report(f"WARNING: no usage log files found matching 'usage*.csv' in {self.logdir}")
        if not content_files:
            report(f"WARNING: no content log files found matching 'content*.jsonl' in {self.logdir}")

        report(f"Usage log family ({len(usage_files)} file(s)): {[f.name for f in usage_files]}")
        report(f"Content log family ({len(content_files)} file(s)): {[f.name for f in content_files]}")
        report_blank()

        usage_df, dupes_collapsed = LogUtils.load_usage_logs(usage_files, dedupe_consecutive=self.dedupe_usage)
        if self.dedupe_usage:
            report(f"Usage log rows loaded: {len(usage_df)} (collapsed {dupes_collapsed} consecutive duplicate rows)")
        else:
            report(f"Usage log rows loaded: {len(usage_df)} (--dedupe-usage not set, no collapsing performed)")

        raw = LogUtils.load_content_logs(content_files)
        cases_raw, turns_raw, events_raw = raw['cases'], raw['turns'], raw['events']
        guesses_raw, legacy_raw, unrecognized = raw['guesses'], raw['legacy'], raw['unrecognized']

        report(f"Content log records: {len(cases_raw)} case-open, {len(turns_raw)} turn, "
               f"{len(events_raw)} event, {len(guesses_raw)} defense-guess, {len(legacy_raw)} legacy")
        if unrecognized:
            report(f"WARNING: {len(unrecognized)} content log line(s) could not be classified -- see unrecognized.jsonl")

        n_no_email = sum(int(f['email'].isna().sum()) for f in (cases_raw, turns_raw, events_raw) if not f.empty)
        if n_no_email:
            report(f"NOTE: {n_no_email} content record(s) had no '_<email>' suffix on their case_id -- "
                   f"these predate the email-splice convention or need review.")

        if self.exclude_emails:
            usage_df, usage_dropped = LogUtils.filter_out_emails(usage_df, self.exclude_emails)
            cases_raw, cases_dropped = LogUtils.filter_out_emails(cases_raw, self.exclude_emails)
            turns_raw, turns_dropped = LogUtils.filter_out_emails(turns_raw, self.exclude_emails)
            events_raw, events_dropped = LogUtils.filter_out_emails(events_raw, self.exclude_emails)
            guesses_raw, _ = LogUtils.filter_out_emails(guesses_raw, self.exclude_emails)
            legacy_raw, _ = LogUtils.filter_out_emails(legacy_raw, self.exclude_emails)
            report(f"Filtered out {len(self.exclude_emails)} email(s) {self.exclude_emails}: "
                   f"removed {usage_dropped} usage row(s), {cases_dropped} case(s), "
                   f"{turns_dropped} turn(s), {events_dropped} event(s)")

        if self.participants is not None:
            usage_df, usage_dropped = ParticipantFilter.apply(usage_df, self.participants)
            cases_raw, cases_dropped = ParticipantFilter.apply(cases_raw, self.participants)
            turns_raw, turns_dropped = ParticipantFilter.apply(turns_raw, self.participants)
            events_raw, events_dropped = ParticipantFilter.apply(events_raw, self.participants)
            guesses_raw, _ = ParticipantFilter.apply(guesses_raw, self.participants)
            legacy_raw, _ = ParticipantFilter.apply(legacy_raw, self.participants)
            report(f"Participants only ({len(self.participants)} listed): "
                   f"removed {usage_dropped} usage row(s), {cases_dropped} case(s), "
                   f"{turns_dropped} turn(s), {events_dropped} event(s) of non-participants "
                   f"or without identity")

        # Derived tables
        turns_df = LogUtils.flag_seeded_turns(turns_raw, events_raw)
        turns_df = LogUtils.add_seconds_since_prev(turns_df, cases_raw, events_raw, guesses_raw)
        critiques_df, unanswered_crit = LogUtils.build_critiques(events_raw)
        pack_timeline = LogUtils.build_pack_timeline(usage_df)
        cases_df = LogUtils.enrich_cases(cases_raw, turns_df, events_raw, guesses_raw, legacy_raw,
                                critiques_df, unanswered_crit, pack_timeline)

        # Turns/events whose case-open record is absent (rotated away, lost, filtered):
        known_cases = set(cases_df['case_key']) if not cases_df.empty else set()
        orphan_cases = ({ck for ck in turns_df['case_key']} | {ck for ck in events_raw['case_key']}) - known_cases
        if orphan_cases:
            report(f"NOTE: {len(orphan_cases)} case(s) have turns/events but no case-open record "
                   f"(not in cases.csv; their turns/events are still in turns.csv/events.csv).")

        user_summary = LogUtils.build_user_summary(usage_df, cases_df, turns_df, events_raw, guesses_raw)
        model_perf = LogUtils.build_model_performance_summary(usage_df)
        action_counts = usage_df['action'].value_counts().rename_axis('action').reset_index(name='rows')
        origin = OriginReport.build(cases_df)

        # ---- report body ----
        report_blank()
        report("Usage rows by action:")
        report(action_counts.to_string(index=False))

        if not cases_df.empty:
            report_blank()
            report(f"Cases: {len(cases_df)}; defenses: "
                   f"{cases_df['defense'].value_counts().to_dict()}")
            if cases_df['experiment_pack'].notna().any():
                report(f"Cases by experiment pack: {cases_df['experiment_pack'].value_counts().to_dict()}")
            guessed = cases_df['guess_correct'].dropna()
            if len(guessed):
                report(f"Defense guesses: {len(guessed)} case(s) guessed, "
                       f"{int(guessed.astype(bool).sum())} exactly matched the case's defense label")
            report(f"Role switches: {int(cases_df['n_role_switches'].sum())} across "
                   f"{int((cases_df['n_role_switches'] > 0).sum())} case(s)")
            report(f"Session Critique: {int(cases_df['n_critique_requests'].sum())} request(s), "
                   f"{int(cases_df['n_critique_ok'].sum())} ok, "
                   f"{int(cases_df['n_critique_failed'].sum())} failed, "
                   f"{int(cases_df['n_critique_unanswered'].sum())} with no result logged")
            if not critiques_df.empty and critiques_df['latency_s'].notna().any():
                lat = critiques_df['latency_s'].dropna()
                report(f"Session Critique request->result latency (s): "
                       f"mean {lat.mean():.1f}, min {lat.min():.1f}, max {lat.max():.1f}")

        if not origin.empty:
            report_blank()
            report("Student origin, by session timezone (students, cases):")
            report(origin.to_string(index=False))

        # Consistency check: every content record should have produced one
        # LOG_CONTENT usage row (case-open, turn, event and guess records).
        if 'usage_log_content_rows' in user_summary.columns and 'content_records_total' in user_summary.columns:
            chk = user_summary[['usage_log_content_rows', 'content_records_total']].dropna()
            if not chk.empty:
                report_blank()
                report("LOG_CONTENT usage rows vs content records, per user (should roughly agree; "
                       "a gap suggests lost log writes or a log window that cuts a session):")
                report(chk.to_string())

        report_blank()
        if model_perf.empty:
            report("No rows with both model and runtime found -- model_performance.csv will be empty (header only).")
        else:
            report(f"Model performance (seconds), by mean runtime ascending:\n{model_perf.to_string()}")

        if not self.exclude_emails and self.participants is None:
            report_blank()
            report("REMINDER: no --filter-email or --participants-file given -- your own test entries "
                   "are likely still mixed into these results. Re-run with --filter-email <your address> "
                   "or --participants-file <experiment_packs.json> to exclude them.")

        return AnalysisResult(
            tables={
                'usage_clean': usage_df,
                'action_counts': action_counts,
                'cases': cases_df,
                'turns': turns_df,
                'events': events_raw,
                'critiques': critiques_df,
                'summary_by_user': user_summary,
                'model_performance': model_perf,
                'origin_by_timezone': origin,
                },
            unrecognized=unrecognized,
            report_lines=lines,
            )

    # ------------------------- Command line --------------------

    @classmethod
    def build_parser(cls):
        parser = argparse.ArgumentParser(description=__doc__,
                                          formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('logdir', help="Directory containing both usage.csv and content.jsonl rotation families")
        parser.add_argument('outdir', help="Directory to write extracted CSVs into")
        parser.add_argument('--dedupe-usage', action='store_true',
                             help="Collapse consecutive duplicate usage-log rows "
                                  "(needed only for logs predating the FileHandler-leak fix; default: off)")
        parser.add_argument('--filter-email', action='extend', nargs='+', default=[], metavar='EMAIL',
                             help="Email address(es) to exclude from all results. Accepts multiple "
                                  "space-separated values and/or repeated flags, e.g. "
                                  "--filter-email me@stanford.edu test@x.com")
        parser.add_argument('--participants-file', default=None, metavar='JSON',
                             help="Restrict all results to the people listed under 'participants' in this "
                                  "experiment_packs.json (emails) or participants.json (pseudonym codes)")
        return parser

    @classmethod
    def main(cls, argv=None):
        args = cls.build_parser().parse_args(argv)
        participants = ParticipantFilter.load(args.participants_file) if args.participants_file else None
        result = cls(args.logdir, dedupe_usage=args.dedupe_usage,
                     exclude_emails=args.filter_email, participants=participants).analyze()
        result.write(args.outdir)
        print(result.report, end='')
        print(f"Wrote {', '.join(name + '.csv' for name in result.tables)}, report.txt to {args.outdir}")


if __name__ == '__main__':
    LogAnalyzer.main()
