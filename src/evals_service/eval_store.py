# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/eval_store.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
eval_store.py

The service's one database: people, device tokens, ratings, and which
cases a person has opened. SQLite in WAL mode, a single file under the
service's data directory. This file holds the only copy of the human
ratings, so back it up (see the install notes).

A submitted rating can never change: a trigger in the database refuses
UPDATE and DELETE on it, no matter what the code does.

All times are UTC, as ISO-8601 text.
'''

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals_service.evals_common import EvalsCommon


class StoreError(Exception):
    '''
    A request the store refuses, with a message fit for the person who made it.
    '''


class EvalStore:
    '''
    Thread-safe access to the SQLite file.
    '''

    SCHEMA_VERSION = 1

    SCHEMA = '''
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS people (
        id            INTEGER PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE,
        name          TEXT,
        label         TEXT NOT NULL UNIQUE,
        roles         TEXT NOT NULL,
        pw_hash       TEXT,
        invite_hash   TEXT,
        invite_expires_utc TEXT,
        created_utc   TEXT NOT NULL,
        activated_utc TEXT,
        last_seen_utc TEXT,
        disabled      INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS device_tokens (
        token_hash    TEXT PRIMARY KEY,
        person_id     INTEGER NOT NULL REFERENCES people(id),
        created_utc   TEXT NOT NULL,
        last_used_utc TEXT NOT NULL,
        expires_utc   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ratings (
        id            INTEGER PRIMARY KEY,
        person_id     INTEGER NOT NULL REFERENCES people(id),
        form_kind     TEXT NOT NULL,
        subject_key   TEXT NOT NULL,
        state         TEXT NOT NULL CHECK (state IN ('draft', 'submitted')),
        answers_json  TEXT NOT NULL,
        rubric_version TEXT NOT NULL,
        started_utc   TEXT NOT NULL,
        updated_utc   TEXT NOT NULL,
        submitted_utc TEXT,
        UNIQUE (person_id, form_kind, subject_key)
    );
    CREATE TABLE IF NOT EXISTS seen (
        person_id     INTEGER NOT NULL REFERENCES people(id),
        form_kind     TEXT NOT NULL,
        subject_key   TEXT NOT NULL,
        first_utc     TEXT NOT NULL,
        last_utc      TEXT NOT NULL,
        PRIMARY KEY (person_id, form_kind, subject_key)
    );
    CREATE TRIGGER IF NOT EXISTS ratings_no_update_after_submit
        BEFORE UPDATE ON ratings WHEN OLD.state = 'submitted'
        BEGIN SELECT RAISE(ABORT, 'a submitted rating cannot be changed'); END;
    CREATE TRIGGER IF NOT EXISTS ratings_no_delete_after_submit
        BEFORE DELETE ON ratings WHEN OLD.state = 'submitted'
        BEGIN SELECT RAISE(ABORT, 'a submitted rating cannot be deleted'); END;
    '''

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, db_path, clock=None):
        '''
        :param db_path: SQLite file; created, with its directory, if missing
        :type db_path: union[str, Path]
        :param clock: returns the current time as an aware datetime; for tests
        :type clock: optional[callable]
        '''
        self._path = Path(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.executescript(self.SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                             (str(self.SCHEMA_VERSION),))

    def close(self):
        with self._lock:
            self._db.close()

    # ------------------------- People -------------------------

    #------------------------------------
    # add_person
    #-------------------

    def add_person(self, email, roles, name=None, label=None, invite_hash=None, invite_valid_days=None):
        '''
        :param email: normalized e-mail address (the login name)
        :type email: str
        :param roles: any of EvalsCommon.ALL_ROLES
        :type roles: iterable[str]
        :param name: real name, shown to the administrator and to instructors
        :type name: optional[str]
        :param label: display label; default 'Evaluator_<n>' for evaluators, else the e-mail
        :type label: optional[str]
        :param invite_hash: Credentials.hash_code of the invite code
        :type invite_hash: optional[str]
        :param invite_valid_days: lifetime of the invite
        :type invite_valid_days: optional[int]
        :return: the new person's id
        :rtype: int
        :raises StoreError: if the e-mail or label exists or a role is unknown
        '''
        roles = sorted(set(roles))
        if not roles or set(roles) - set(EvalsCommon.ALL_ROLES):
            raise StoreError(f"roles must be from {EvalsCommon.ALL_ROLES}")
        now = self._now()
        expires = self._iso(self._clock() + timedelta(days=invite_valid_days or EvalsCommon.INVITE_VALID_DAYS)) \
            if invite_hash else None
        with self._lock:
            if self.person_by_email(email) is not None:
                raise StoreError(f"{email} already has an account")
            if label is None:
                label = self._next_evaluator_label() if EvalsCommon.ROLE_EVALUATOR in roles else email
            try:
                cursor = self._db.execute(
                    'INSERT INTO people (email, name, label, roles, invite_hash, invite_expires_utc, created_utc) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (email, name, label, ','.join(roles), invite_hash, expires, now))
            except sqlite3.IntegrityError as e:
                raise StoreError(f"label '{label}' is already used") from e
            return cursor.lastrowid

    #------------------------------------
    # set_invite
    #-------------------

    def set_invite(self, person_id, invite_hash, valid_days=None):
        '''
        Issue a new invite code, e.g. after a forgotten password. The old password stays
        valid until the new code is used.

        :param person_id: whose invite
        :type person_id: int
        :param invite_hash: Credentials.hash_code of the code
        :type invite_hash: str
        :param valid_days: lifetime
        :type valid_days: optional[int]
        '''
        expires = self._iso(self._clock() + timedelta(days=valid_days or EvalsCommon.INVITE_VALID_DAYS))
        with self._lock:
            self._db.execute('UPDATE people SET invite_hash=?, invite_expires_utc=? WHERE id=?',
                             (invite_hash, expires, person_id))

    #------------------------------------
    # activate
    #-------------------

    def activate(self, person_id, pw_hash):
        '''
        Set the password and consume the invite.

        :param person_id: whose account
        :type person_id: int
        :param pw_hash: Credentials.hash_password of the new password
        :type pw_hash: str
        '''
        now = self._now()
        with self._lock:
            self._db.execute(
                'UPDATE people SET pw_hash=?, invite_hash=NULL, invite_expires_utc=NULL, '
                'activated_utc=COALESCE(activated_utc, ?), last_seen_utc=? WHERE id=?',
                (pw_hash, now, now, person_id))

    #------------------------------------
    # set_disabled
    #-------------------

    def set_disabled(self, person_id, disabled):
        '''
        A disabled account cannot log in and its device tokens stop working.

        :param person_id: whose account
        :type person_id: int
        :param disabled: new state
        :type disabled: bool
        '''
        with self._lock:
            self._db.execute('UPDATE people SET disabled=? WHERE id=?', (1 if disabled else 0, person_id))

    #------------------------------------
    # person_by_email
    #-------------------

    def person_by_email(self, email):
        '''
        :param email: normalized address
        :type email: str
        :return: the row as a dict, or None
        :rtype: optional[dict]
        '''
        with self._lock:
            return self._one('SELECT * FROM people WHERE email=?', (email,))

    def person_by_id(self, person_id):
        '''
        :param person_id: id
        :type person_id: int
        :return: the row as a dict, or None
        :rtype: optional[dict]
        '''
        with self._lock:
            return self._one('SELECT * FROM people WHERE id=?', (person_id,))

    def all_people(self):
        '''
        :return: every person, ordered by id
        :rtype: list[dict]
        '''
        with self._lock:
            return [dict(r) for r in self._db.execute('SELECT * FROM people ORDER BY id')]

    #------------------------------------
    # touch
    #-------------------

    def touch(self, person_id):
        '''
        Record that the person is active now.

        :param person_id: who
        :type person_id: int
        '''
        with self._lock:
            self._db.execute('UPDATE people SET last_seen_utc=? WHERE id=?', (self._now(), person_id))

    # ------------------------- Device tokens -------------------------

    #------------------------------------
    # add_device_token
    #-------------------

    def add_device_token(self, person_id, token_hash):
        '''
        :param person_id: who the browser belongs to
        :type person_id: int
        :param token_hash: Credentials.hash_token of the token given to the browser
        :type token_hash: str
        '''
        now = self._clock()
        with self._lock:
            self._db.execute(
                'INSERT INTO device_tokens (token_hash, person_id, created_utc, last_used_utc, expires_utc) '
                'VALUES (?, ?, ?, ?, ?)',
                (token_hash, person_id, self._iso(now), self._iso(now),
                 self._iso(now + timedelta(days=EvalsCommon.DEVICE_TOKEN_VALID_DAYS))))

    #------------------------------------
    # person_for_token
    #-------------------

    def person_for_token(self, token_hash):
        '''
        Look up the owner of a device token and extend the token's life.

        :param token_hash: Credentials.hash_token of what the browser sent
        :type token_hash: str
        :return: the person, or None if the token is unknown, expired or the account is disabled
        :rtype: optional[dict]
        '''
        now = self._clock()
        with self._lock:
            row = self._one('SELECT * FROM device_tokens WHERE token_hash=?', (token_hash,))
            if row is None or row['expires_utc'] <= self._iso(now):
                return None
            person = self.person_by_id(row['person_id'])
            if person is None or person['disabled'] or person['pw_hash'] is None:
                return None
            self._db.execute('UPDATE device_tokens SET last_used_utc=?, expires_utc=? WHERE token_hash=?',
                             (self._iso(now), self._iso(now + timedelta(days=EvalsCommon.DEVICE_TOKEN_VALID_DAYS)),
                              token_hash))
            self._db.execute('UPDATE people SET last_seen_utc=? WHERE id=?', (self._iso(now), person['id']))
            return person

    def drop_device_token(self, token_hash):
        '''
        :param token_hash: the token to forget (log out of one browser)
        :type token_hash: str
        '''
        with self._lock:
            self._db.execute('DELETE FROM device_tokens WHERE token_hash=?', (token_hash,))

    def drop_all_device_tokens(self, person_id):
        '''
        :param person_id: whose browsers to log out
        :type person_id: int
        '''
        with self._lock:
            self._db.execute('DELETE FROM device_tokens WHERE person_id=?', (person_id,))

    # ------------------------- Ratings -------------------------

    #------------------------------------
    # get_rating
    #-------------------

    def get_rating(self, person_id, form_kind, subject_key):
        '''
        :param person_id: rater
        :type person_id: int
        :param form_kind: one of EvalsCommon.ALL_FORMS
        :type form_kind: str
        :param subject_key: what is rated (session id, or milestone|student id)
        :type subject_key: str
        :return: row with 'answers' decoded, or None
        :rtype: optional[dict]
        '''
        with self._lock:
            row = self._one('SELECT * FROM ratings WHERE person_id=? AND form_kind=? AND subject_key=?',
                            (person_id, form_kind, subject_key))
        return self._decode(row)

    #------------------------------------
    # save_draft
    #-------------------

    def save_draft(self, person_id, form_kind, subject_key, answers, rubric_version):
        '''
        Create or replace a draft.

        :param person_id: rater
        :type person_id: int
        :param form_kind: form kind
        :type form_kind: str
        :param subject_key: what is rated
        :type subject_key: str
        :param answers: cleaned answers (see RubricLibrary.clean_answers)
        :type answers: dict
        :param rubric_version: RubricLibrary.version
        :type rubric_version: str
        :return: the stored row
        :rtype: dict
        :raises StoreError: if the rating was already submitted
        '''
        return self._write(person_id, form_kind, subject_key, answers, rubric_version, submit=False)

    #------------------------------------
    # submit
    #-------------------

    def submit(self, person_id, form_kind, subject_key, answers, rubric_version):
        '''
        Store the final rating. It cannot be changed afterwards.

        :param person_id: rater
        :type person_id: int
        :param form_kind: form kind
        :type form_kind: str
        :param subject_key: what is rated
        :type subject_key: str
        :param answers: cleaned, complete answers
        :type answers: dict
        :param rubric_version: RubricLibrary.version
        :type rubric_version: str
        :return: the stored row
        :rtype: dict
        :raises StoreError: if the rating was already submitted
        '''
        return self._write(person_id, form_kind, subject_key, answers, rubric_version, submit=True)

    #------------------------------------
    # ratings_of
    #-------------------

    def ratings_of(self, person_id=None, form_kind=None):
        '''
        :param person_id: only this rater's ratings; None for all
        :type person_id: optional[int]
        :param form_kind: only this form; None for all
        :type form_kind: optional[str]
        :return: rows without the answers, ordered by update time
        :rtype: list[dict]
        '''
        sql = ('SELECT id, person_id, form_kind, subject_key, state, rubric_version, started_utc, updated_utc, '
               'submitted_utc FROM ratings WHERE 1=1')
        args = []
        if person_id is not None:
            sql += ' AND person_id=?'
            args.append(person_id)
        if form_kind is not None:
            sql += ' AND form_kind=?'
            args.append(form_kind)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql + ' ORDER BY updated_utc', args)]

    #------------------------------------
    # export_submitted
    #-------------------

    def export_submitted(self):
        '''
        :return: every submitted rating with its answers decoded, joined with the rater's label
        :rtype: list[dict]
        '''
        with self._lock:
            rows = self._db.execute(
                'SELECT r.*, p.label AS rater_label, p.email AS rater_email FROM ratings r '
                'JOIN people p ON p.id = r.person_id WHERE r.state = ? ORDER BY r.id',
                (EvalsCommon.STATE_SUBMITTED,)).fetchall()
        return [self._decode(dict(r)) for r in rows]

    # ------------------------- Seen -------------------------

    def mark_seen(self, person_id, form_kind, subject_key):
        '''
        Remember that the person opened this item.

        :param person_id: who
        :type person_id: int
        :param form_kind: form kind
        :type form_kind: str
        :param subject_key: what was opened
        :type subject_key: str
        '''
        now = self._now()
        with self._lock:
            self._db.execute(
                'INSERT INTO seen (person_id, form_kind, subject_key, first_utc, last_utc) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT (person_id, form_kind, subject_key) DO UPDATE SET last_utc=excluded.last_utc',
                (person_id, form_kind, subject_key, now, now))

    def seen_of(self, person_id, form_kind=None):
        '''
        :param person_id: who
        :type person_id: int
        :param form_kind: restrict to one form
        :type form_kind: optional[str]
        :return: the subject keys the person opened
        :rtype: set[str]
        '''
        sql, args = 'SELECT subject_key FROM seen WHERE person_id=?', [person_id]
        if form_kind is not None:
            sql += ' AND form_kind=?'
            args.append(form_kind)
        with self._lock:
            return {r[0] for r in self._db.execute(sql, args)}

    # ------------------------- Private -------------------------

    def _write(self, person_id, form_kind, subject_key, answers, rubric_version, submit):
        now = self._now()
        state = EvalsCommon.STATE_SUBMITTED if submit else EvalsCommon.STATE_DRAFT
        with self._lock:
            existing = self._one('SELECT * FROM ratings WHERE person_id=? AND form_kind=? AND subject_key=?',
                                 (person_id, form_kind, subject_key))
            if existing is not None and existing['state'] == EvalsCommon.STATE_SUBMITTED:
                raise StoreError("This rating was already submitted and cannot be changed.")
            blob = json.dumps(answers, sort_keys=True, ensure_ascii=False)
            if existing is None:
                self._db.execute(
                    'INSERT INTO ratings (person_id, form_kind, subject_key, state, answers_json, rubric_version, '
                    'started_utc, updated_utc, submitted_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (person_id, form_kind, subject_key, state, blob, rubric_version, now, now,
                     now if submit else None))
            else:
                self._db.execute(
                    'UPDATE ratings SET state=?, answers_json=?, rubric_version=?, updated_utc=?, submitted_utc=? '
                    'WHERE id=?', (state, blob, rubric_version, now, now if submit else None, existing['id']))
            self._db.execute('UPDATE people SET last_seen_utc=? WHERE id=?', (now, person_id))
            return self.get_rating(person_id, form_kind, subject_key)

    def _next_evaluator_label(self):
        taken = {r[0] for r in self._db.execute('SELECT label FROM people')}
        n = 1
        while f'{EvalsCommon.EVALUATOR_LABEL_PREFIX}{n}' in taken:
            n += 1
        return f'{EvalsCommon.EVALUATOR_LABEL_PREFIX}{n}'

    def _one(self, sql, args):
        row = self._db.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        row = dict(row)
        row['answers'] = json.loads(row.pop('answers_json'))
        return row

    def now_iso(self):
        '''
        :return: the current time as the store writes it
        :rtype: str
        '''
        return self._now()

    def _iso(self, moment):
        return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _now(self):
        return self._iso(self._clock())
