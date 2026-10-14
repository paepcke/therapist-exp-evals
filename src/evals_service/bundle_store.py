# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/bundle_store.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
bundle_store.py

The sessions that evaluators review. The trainer's server builds a "bundle"
and pushes it into the inbox as two files:

    sessions-<id>.jsonl    one JSON object per session (pushed first)
    manifest-<id>.json     describes the bundle (pushed last: it is the commit marker)

A manifest names its sessions file and carries the file's SHA-256, so a
half-pushed or damaged bundle is never activated. This class installs the
newest valid bundle under releases/<id>/, points the 'current' link in releases/ at it
atomically, and serves sessions from there. A bad bundle is logged and
ignored; the previous one stays in service.

A session object:

    {"session_id": "16 hex", "student_code": "8 hex", "day": "2026-10-03",
     "created_utc": "...Z", "client_name": "...", "defense": "projection",
     "html": "<section class=\\"ev-case\\">...</section>",
     "critiques": ["plain text of one Session Critique", ...]}

The HTML is made on the trainer's server by SessionRenderer, which escapes
all text. As a second line of defence this class refuses bundles whose HTML
holds scripts, frames, event handlers or anything that looks like an e-mail
address.
'''

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from evals_service.evals_common import EvalsCommon


class BundleError(Exception):
    '''
    A bundle that must not be activated.
    '''


class BundleStore:
    '''
    Installs bundles from the inbox and serves their sessions.
    '''

    SESSION_ID_RE = re.compile(r'^[0-9a-f]{16}$')
    CODE_RE = re.compile(r'^[0-9a-f]{8}$')
    BUNDLE_ID_RE = re.compile(r'^[A-Za-z0-9T_.-]{1,64}$')
    EMAIL_LIKE_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}')
    DANGEROUS_RE = re.compile(r'<\s*(script|iframe|object|embed|link|meta|base|form)\b|\bon[a-z]+\s*=|javascript:',
                              re.IGNORECASE)
    MAX_HTML_BYTES = 2_000_000
    REQUIRED_KEYS = ('session_id', 'student_code', 'day', 'created_utc', 'html')

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, inbox_dir=None, service_root=None, clock=None):
        '''
        :param inbox_dir: where bundles arrive
        :type inbox_dir: optional[union[str, Path]]
        :param service_root: contains releases/
        :type service_root: optional[union[str, Path]]
        :param clock: returns seconds as time.monotonic does; for tests
        :type clock: optional[callable]
        '''
        self._inbox = Path(inbox_dir or EvalsCommon.INBOX_DIR)
        root = Path(service_root or EvalsCommon.SERVICE_ROOT)
        self._releases = root / EvalsCommon.RELEASES_DIR_NAME
        self._current = self._releases / EvalsCommon.CURRENT_LINK_NAME
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._log = logging.getLogger('evals_service')
        self._sessions = {}
        self._info = None
        self._last_scan = None
        self._bad = {}            # manifest path -> mtime of the one that failed
        self._releases.mkdir(parents=True, exist_ok=True)
        self._load_current()

    #------------------------------------
    # refresh
    #-------------------

    def refresh(self, force=False):
        '''
        Look for a newer bundle in the inbox and activate it. Cheap to call
        often: scans at most every EvalsCommon.BUNDLE_RESCAN_SECS.

        :param force: scan now
        :type force: bool
        :return: True if a different bundle became active
        :rtype: bool
        '''
        with self._lock:
            now = self._clock()
            if not force and self._last_scan is not None \
                    and now - self._last_scan < EvalsCommon.BUNDLE_RESCAN_SECS:
                return False
            self._last_scan = now
            candidates = self._candidates()
            if not candidates:
                return False
            built, bundle_id, manifest_path = candidates[-1]
            if self._info is not None and (self._info['built_utc'], self._info['bundle_id']) >= (built, bundle_id):
                return False
            try:
                self._install(manifest_path)
            except (BundleError, OSError, ValueError) as e:
                self._bad[str(manifest_path)] = self._mtime(manifest_path)
                self._log.error("Bundle %s was not activated: %s", manifest_path.name, e)
                return False
            return True

    #------------------------------------
    # info
    #-------------------

    def info(self):
        '''
        :return: manifest facts of the active bundle, or None if there is none yet
        :rtype: optional[dict]
        '''
        with self._lock:
            return dict(self._info) if self._info else None

    #------------------------------------
    # session
    #-------------------

    def session(self, session_id):
        '''
        :param session_id: id
        :type session_id: str
        :return: the session object, or None
        :rtype: optional[dict]
        '''
        with self._lock:
            return self._sessions.get(session_id)

    def session_ids(self):
        '''
        :return: all session ids of the active bundle, in bundle order
        :rtype: list[str]
        '''
        with self._lock:
            return list(self._sessions)

    # ------------------------- Private -------------------------

    def _candidates(self):
        found = []
        if not self._inbox.is_dir():
            return found
        for path in self._inbox.glob(EvalsCommon.MANIFEST_GLOB):
            if self._bad.get(str(path)) == self._mtime(path):
                continue
            try:
                doc = json.loads(path.read_text(encoding='utf-8'))
                found.append((str(doc['built_utc']), str(doc['bundle_id']), path))
            except (OSError, ValueError, KeyError, TypeError) as e:
                self._bad[str(path)] = self._mtime(path)
                self._log.error("Unreadable manifest %s: %s", path.name, e)
        return sorted(found)

    def _install(self, manifest_path):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        bundle_id = str(manifest.get('bundle_id', ''))
        if not self.BUNDLE_ID_RE.match(bundle_id):
            raise BundleError("bad bundle_id")
        if manifest.get('format_version') != EvalsCommon.BUNDLE_FORMAT_VERSION:
            raise BundleError(f"format version {manifest.get('format_version')!r}, "
                              f"expected {EvalsCommon.BUNDLE_FORMAT_VERSION}")
        if manifest.get('role_colors') != EvalsCommon.ROLE_COLORS:
            raise BundleError("role colors differ from this service's")
        if manifest_path.name != EvalsCommon.MANIFEST_FILE_FORMAT.format(bundle_id=bundle_id):
            raise BundleError("manifest file name does not match its bundle_id")
        sessions_name = EvalsCommon.SESSIONS_FILE_FORMAT.format(bundle_id=bundle_id)
        sessions_path = manifest_path.parent / sessions_name
        raw = sessions_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest.get('sessions_sha256'):
            raise BundleError("sessions file does not match the manifest's checksum")
        sessions = self._parse(raw)
        if manifest.get('session_count') != len(sessions):
            raise BundleError(f"manifest says {manifest.get('session_count')} sessions, file has {len(sessions)}")

        target = self._releases / bundle_id
        staging = self._releases / f'.staging-{bundle_id}-{os.getpid()}'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        (staging / sessions_name).write_bytes(raw)
        (staging / manifest_path.name).write_bytes(manifest_path.read_bytes())
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
        link = self._releases / f'.link-{os.getpid()}'
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(bundle_id, link)
        os.replace(link, self._current)
        self._sessions = sessions
        self._info = {'bundle_id': bundle_id, 'built_utc': str(manifest['built_utc']),
                      'session_count': len(sessions),
                      'participant_count': len({s['student_code'] for s in sessions.values()})}
        self._log.info("Activated bundle %s with %d sessions", bundle_id, len(sessions))
        self._prune()

    def _parse(self, raw):
        sessions = {}
        for number, line in enumerate(raw.decode('utf-8').splitlines(), start=1):
            if not line.strip():
                continue
            try:
                s = json.loads(line)
            except ValueError as e:
                raise BundleError(f"line {number}: not JSON") from e
            self._check_session(s, number)
            if s['session_id'] in sessions:
                raise BundleError(f"line {number}: duplicate session_id")
            s['critiques'] = [str(c) for c in s.get('critiques', [])]
            s['client_name'] = str(s.get('client_name') or '')
            s['defense'] = str(s.get('defense') or '')
            sessions[s['session_id']] = s
        return sessions

    def _check_session(self, s, number):
        for key in self.REQUIRED_KEYS:
            if key not in s:
                raise BundleError(f"line {number}: missing '{key}'")
        if not self.SESSION_ID_RE.match(str(s['session_id'])) or not self.CODE_RE.match(str(s['student_code'])):
            raise BundleError(f"line {number}: malformed id or code")
        if len(s['html'].encode('utf-8')) > self.MAX_HTML_BYTES:
            raise BundleError(f"line {number}: html too large")
        texts = [s['html'], s.get('client_name') or '', s.get('defense') or ''] + [str(c) for c in s.get('critiques', [])]
        for text in texts:
            if self.DANGEROUS_RE.search(text):
                raise BundleError(f"line {number}: active content in session text")
            if self.EMAIL_LIKE_RE.search(text):
                raise BundleError(f"line {number}: e-mail address in session text")

    def _load_current(self):
        if not self._current.is_symlink():
            return
        try:
            bundle_id = os.readlink(self._current)
            manifest_path = self._current / EvalsCommon.MANIFEST_FILE_FORMAT.format(bundle_id=bundle_id)
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            raw = (self._current / EvalsCommon.SESSIONS_FILE_FORMAT.format(bundle_id=bundle_id)).read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest['sessions_sha256']:
                raise BundleError("installed release is damaged")
            self._sessions = self._parse(raw)
            self._info = {'bundle_id': bundle_id, 'built_utc': str(manifest['built_utc']),
                          'session_count': len(self._sessions),
                          'participant_count': len({s['student_code'] for s in self._sessions.values()})}
        except (OSError, ValueError, KeyError, BundleError) as e:
            self._log.error("Could not load the installed release: %s", e)
            self._sessions, self._info = {}, None

    def _prune(self):
        releases = sorted((p for p in self._releases.iterdir() if p.is_dir() and not p.is_symlink()
                           and not p.name.startswith('.')), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in releases[EvalsCommon.RELEASES_KEPT:]:
            shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _mtime(path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None
