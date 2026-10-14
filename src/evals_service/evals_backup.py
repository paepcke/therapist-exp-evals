# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-13 17:26:44
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/evals_backup.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-13 17:26:44
# **********************************************************

'''
evals_backup.py

A consistent snapshot of the evaluation database, made while the service runs.
Run nightly by a systemd timer (deploy/thtrainer-evals-backup.timer) as the
service's own account. Each run writes, under the backup directory (default <service root>/backup/):

    evals-<stamp>.sqlite   the database (SQLite's online backup, integrity-checked)
    ratings-<stamp>.csv    the submitted ratings as CSV, readable without this code
    roster.json            the current roster
    LATEST.json            names the newest complete snapshot, with its SHA-256

LATEST.json is written last, so whoever copies the directory can tell a complete
snapshot from a half-written one. The newest EvalsCommon.BACKUP_KEEP snapshots stay.
The laptop that pulls the directory (backup_pull) checks the SHA-256 and the age.

    python -m evals_service.evals_backup [--service-root /srv/evals] [--backup-dir /srv/evals-backup] [--keep 14]
'''

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals_service.eval_store import EvalStore
from evals_service.evals_common import EvalsCommon


class BackupError(Exception):
    '''
    The snapshot could not be made, or failed its check.
    '''


class EvalsBackup:
    '''
    Makes and prunes snapshots.
    '''

    STAMP_FORMAT = '%Y%m%dT%H%M%SZ'
    LOG = logging.getLogger('evals_service')

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, service_root=None, clock=None, keep=None, backup_dir=None):
        '''
        :param service_root: the service's directory
        :type service_root: optional[union[str, Path]]
        :param clock: returns the current time as an aware datetime; for tests
        :type clock: optional[callable]
        :param keep: how many snapshots to keep
        :type keep: optional[int]
        :param backup_dir: where snapshots go; default <service root>/backup. In production a
            directory outside the service root that the pulling account can read
        :type backup_dir: optional[union[str, Path]]
        '''
        self.root = Path(service_root or EvalsCommon.SERVICE_ROOT)
        self.db_path = self.root / EvalsCommon.DATA_DIR_NAME / EvalsCommon.DB_NAME
        self.dir = Path(backup_dir) if backup_dir else self.root / EvalsCommon.BACKUP_DIR_NAME
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.keep = keep or EvalsCommon.BACKUP_KEEP

    #------------------------------------
    # run
    #-------------------

    def run(self):
        '''
        Make one snapshot and prune old ones.

        :return: what LATEST.json now says
        :rtype: dict
        :raises BackupError: if there is no database, or the copy is damaged
        '''
        if not self.db_path.is_file():
            raise BackupError(f"No database at {self.db_path}")
        self.dir.mkdir(parents=True, exist_ok=True)
        created = self.clock().astimezone(timezone.utc)
        stamp = created.strftime(self.STAMP_FORMAT)
        snapshot = self.dir / EvalsCommon.BACKUP_SNAPSHOT_FORMAT.format(stamp=stamp)
        csv_path = self.dir / EvalsCommon.BACKUP_CSV_FORMAT.format(stamp=stamp)
        partial = self.dir / f'.partial-{stamp}.sqlite'
        try:
            self._copy_database(partial)
            counts = self._check_and_count(partial)
            csv_text = self._csv_of(partial)
            self._remove_sidecars(partial)
            digest = self._sha256(partial)
            csv_path.write_text(csv_text, encoding='utf-8')
            os.replace(partial, snapshot)
        finally:
            partial.unlink(missing_ok=True)
            self._remove_sidecars(partial)

        roster = self.root / EvalsCommon.CONFIG_DIR_NAME / EvalsCommon.ROSTER_NAME
        if roster.is_file():
            shutil.copyfile(roster, self.dir / EvalsCommon.BACKUP_ROSTER_COPY)
        latest = {'format': EvalsCommon.BACKUP_FORMAT_VERSION, 'file': snapshot.name, 'csv': csv_path.name,
                  'sha256': digest, 'created_utc': created.strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'people': counts['people'], 'ratings_submitted': counts['submitted'],
                  'ratings_draft': counts['draft']}
        self._write_latest(latest)
        self._prune()
        self.LOG.info("Backup %s: %d people, %d submitted ratings", snapshot.name,
                      counts['people'], counts['submitted'])
        return latest

    # ------------------------- Private -------------------------

    def _copy_database(self, target):
        source = sqlite3.connect(str(self.db_path), timeout=30)
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()

    def _check_and_count(self, path):
        db = sqlite3.connect(str(path))
        try:
            result = db.execute('PRAGMA integrity_check').fetchall()
            if result != [('ok',)]:
                raise BackupError(f"The copy failed its integrity check: {result[:3]}")
            counts = {'people': db.execute('SELECT COUNT(*) FROM people').fetchone()[0]}
            for state in (EvalsCommon.STATE_SUBMITTED, EvalsCommon.STATE_DRAFT):
                key = 'submitted' if state == EvalsCommon.STATE_SUBMITTED else 'draft'
                counts[key] = db.execute('SELECT COUNT(*) FROM ratings WHERE state = ?', (state,)).fetchone()[0]
            return counts
        finally:
            db.close()

    @staticmethod
    def _csv_of(path):
        store = EvalStore(path)
        try:
            return store.ratings_csv()
        finally:
            store.close()

    @staticmethod
    def _remove_sidecars(path):
        for suffix in ('-wal', '-shm'):
            Path(str(path) + suffix).unlink(missing_ok=True)

    @staticmethod
    def _sha256(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
        return h.hexdigest()

    def _write_latest(self, latest):
        tmp = self.dir / '.LATEST.tmp'
        tmp.write_text(json.dumps(latest, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, self.dir / EvalsCommon.BACKUP_LATEST_FILE)

    def _prune(self):
        pattern = EvalsCommon.BACKUP_SNAPSHOT_FORMAT.format(stamp='*')
        snapshots = sorted(self.dir.glob(pattern))
        for old in snapshots[:-self.keep]:
            stamp = old.name[len('evals-'):-len('.sqlite')]
            old.unlink(missing_ok=True)
            (self.dir / EvalsCommon.BACKUP_CSV_FORMAT.format(stamp=stamp)).unlink(missing_ok=True)

    #------------------------------------
    # main
    #-------------------

    @classmethod
    def main(cls, argv=None):
        '''
        Command line entry.

        :param argv: arguments without program name
        :type argv: optional[list[str]]
        :return: exit code, 0 if the snapshot was made
        :rtype: int
        '''
        parser = argparse.ArgumentParser(description='Snapshot the evaluation database.')
        parser.add_argument('--service-root', default=str(EvalsCommon.SERVICE_ROOT))
        parser.add_argument('--backup-dir', default=None, help='default <service root>/backup')
        parser.add_argument('--keep', type=int, default=EvalsCommon.BACKUP_KEEP)
        args = parser.parse_args(argv)
        logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
        try:
            cls(args.service_root, keep=args.keep, backup_dir=args.backup_dir).run()
        except (BackupError, sqlite3.Error, OSError) as e:
            print(f"Backup failed: {e}", file=sys.stderr)
            return 1
        return 0


if __name__ == '__main__':
    sys.exit(EvalsBackup.main())
