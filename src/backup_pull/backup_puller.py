# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-13 17:26:44
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/backup_pull/backup_puller.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-14 20:04:23
# **********************************************************

'''
backup_puller.py

Runs on the administrator's laptop (sparky). Pulls what must not be lost from the
two machines that hold it, checks it, and tells the administrator when a backup
has gone stale:

    vultr-evals     the evaluation database snapshots (evals_service.evals_backup),
                    through a key that can only read the snapshot directory
    quatro-secrets  the pseudonym secret and the sampling state on the trainer's server

A launchd job starts this every hour (deploy/com.thtrainer.backup-pull.plist). A source
is pulled again only when its last success is older than retry_after_hours, so a laptop
that was asleep or off the Stanford VPN simply tries again at the next hour. Only when a
source's data is older than alert_after_hours does the administrator get a macOS
notification and a push message to the phone (through ntfy.sh), and an e-mail if one is
set up in the configuration, at most once per alert_repeat_hours per source, until it is
fixed.

    python -m backup_pull.backup_puller [--config FILE] [--force] [--status] [--test-alert]

Configuration: ~/.thtrainer/backup_pull.json (example in deploy/backup_pull.example.json).
'''

import argparse
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import smtplib
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path


class VerifyError(Exception):
    '''
    A pulled backup is incomplete or damaged.
    '''


class PullConfig:
    '''
    The settings, from a JSON file. Every key has a default except the sources.
    '''

    DEFAULT_PATH = Path.home() / '.thtrainer' / 'backup_pull.json'
    DEFAULTS = {
        'dest_root': '~/Backups/thtrainer',
        'retry_after_hours': 12,
        'alert_after_hours': 48,
        'alert_repeat_hours': 24,
        'timeout_secs': 600,
        # The rsync program. macOS ships openrsync, which the cloud machine's rrsync
        # refuses; use rsync 3.x (brew install rsync) there, e.g. /opt/homebrew/bin/rsync.
        'rsync': 'rsync',
        # Push messages to the phone through ntfy (https://ntfy.sh). The topic name is the
        # secret: anybody who knows it can read the messages, so make it long and random.
        'ntfy': {'server': 'https://ntfy.sh', 'topic': ''},
        # E-mail is optional: it is sent only when 'user' and 'to' are set.
        'mail': {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'user': '',
            'keychain_service': 'thtrainer-backup-smtp',
            'to': '',
        },
    }

    def __init__(self, doc):
        '''
        :param doc: the parsed JSON
        :type doc: dict
        :raises ValueError: for a missing or malformed 'sources'
        '''
        merged = json.loads(json.dumps(self.DEFAULTS))
        for key, value in doc.items():
            if key in ('mail', 'ntfy'):
                merged[key].update(value)
            else:
                merged[key] = value
        self.dest_root = Path(os.path.expanduser(merged['dest_root']))
        self.retry_after = timedelta(hours=float(merged['retry_after_hours']))
        self.alert_after = timedelta(hours=float(merged['alert_after_hours']))
        self.alert_repeat = timedelta(hours=float(merged['alert_repeat_hours']))
        self.timeout_secs = int(merged['timeout_secs'])
        self.rsync = os.path.expanduser(merged['rsync'])
        self.mail = merged['mail']
        self.ntfy = merged['ntfy']
        sources = merged.get('sources')
        if not sources or not isinstance(sources, list):
            raise ValueError("config needs a non-empty list 'sources'")
        self.sources = []
        for s in sources:
            for key in ('name', 'remotes'):
                if key not in s:
                    raise ValueError(f"source without '{key}': {s}")
            if s.get('verify', 'none') not in ('none', 'evals_snapshot'):
                raise ValueError(f"unknown verify kind in source {s['name']}")
            self.sources.append(s)

    @classmethod
    def load(cls, path=None):
        '''
        :param path: config file; default ~/.thtrainer/backup_pull.json
        :type path: optional[union[str, Path]]
        :return: the settings
        :rtype: PullConfig
        '''
        return cls(json.loads(Path(path or cls.DEFAULT_PATH).read_text(encoding='utf-8')))


class StatusFile:
    '''
    What happened to each source, kept between runs: {"<source>": {...}}.
    Times are ISO strings in UTC.
    '''

    def __init__(self, path):
        self.path = Path(path)
        self.sources = {}
        if self.path.is_file():
            try:
                self.sources = json.loads(self.path.read_text(encoding='utf-8'))
            except ValueError:
                self.sources = {}

    def of(self, name):
        return self.sources.setdefault(name, {})

    def save(self):
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(self.sources, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, self.path)

    @staticmethod
    def when(text):
        '''
        :param text: ISO time as stored, or None
        :type text: optional[str]
        :return: aware datetime, or None
        :rtype: optional[datetime]
        '''
        return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc) if text else None

    @staticmethod
    def text(moment):
        return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class RsyncRunner:
    '''
    Runs one rsync pull. Replaceable in tests.
    '''

    SSH_OPTIONS = ('BatchMode=yes', 'ConnectTimeout=15', 'ServerAliveInterval=15', 'ServerAliveCountMax=3')

    def __init__(self, timeout_secs=600, rsync='rsync'):
        self.timeout_secs = timeout_secs
        self.rsync = rsync

    def pull(self, remote, dest, ssh_key=None):
        '''
        :param remote: rsync source, e.g. 'user@host:path'; ends in '/' for a directory
        :type remote: str
        :param dest: local directory
        :type dest: Path
        :param ssh_key: private key to offer, if not the defaults
        :type ssh_key: optional[str]
        :raises RuntimeError: with rsync's message when it fails or takes too long
        '''
        ssh = ['ssh'] + [x for o in self.SSH_OPTIONS for x in ('-o', o)]
        if ssh_key:
            ssh += ['-i', os.path.expanduser(ssh_key), '-o', 'IdentitiesOnly=yes']
        cmd = [self.rsync, '-r', '-t', '-e', ' '.join(ssh), remote, str(dest) + '/']
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_secs, check=False)
        except FileNotFoundError as e:
            raise RuntimeError(f"cannot run rsync: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"rsync took longer than {self.timeout_secs} seconds") from e
        if done.returncode != 0:
            message = f"rsync exited with {done.returncode}: {(done.stderr or '').strip()[-300:]}"
            if 'invalid rsync-command' in (done.stderr or ''):
                message += (" -- The macOS rsync (openrsync) is not accepted by the cloud machine's rrsync. "
                            "Install rsync 3.x (brew install rsync) and set \"rsync\" in the config "
                            "to its full path.")
            raise RuntimeError(message)


class Notifier:
    '''
    Tells the administrator, over every channel that is set up: a macOS notification,
    a push message to the phone (ntfy), and optionally an e-mail. Replaceable in tests.
    '''

    def __init__(self, mail, ntfy=None):
        '''
        :param mail: the 'mail' section of the config
        :type mail: dict
        :param ntfy: the 'ntfy' section of the config
        :type ntfy: optional[dict]
        '''
        self.mail = mail
        self.ntfy = ntfy or {}
        self.log = logging.getLogger('backup_pull')

    def send(self, subject, body, short=None):
        '''
        :param subject: short text
        :type subject: str
        :param body: longer text, for e-mail
        :type body: str
        :param short: text for the phone; default the body's first line
        :type short: optional[str]
        :return: True if at least one channel worked
        :rtype: bool
        '''
        return any(v for v in self.send_each(subject, body, short).values())

    def send_each(self, subject, body, short=None):
        '''
        :return: for each channel True (sent), False (failed) or None (not set up)
        :rtype: dict[str, optional[bool]]
        '''
        first_line = body.splitlines()[0] if body else ''
        return {'notification': self._notification(subject, first_line),
                'push': self._push(subject, short if short is not None else first_line),
                'email': self._email(subject, body)}

    def _notification(self, title, text):
        script = 'display notification "{}" with title "{}"'.format(self._q(text), self._q(title))
        try:
            subprocess.run(['osascript', '-e', script], capture_output=True, timeout=30, check=True)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            self.log.warning("Notification failed: %s", e)
            return False

    @staticmethod
    def _q(text):
        return text.replace('\\', '\\\\').replace('"', '\\"')

    def _push(self, title, text):
        topic = self.ntfy.get('topic')
        if not topic:
            return None
        url = f"{self.ntfy.get('server', 'https://ntfy.sh').rstrip('/')}/{topic}"
        request = urllib.request.Request(url, data=text.encode('utf-8'), method='POST',
                                         headers={'Title': title, 'Priority': 'high', 'Tags': 'warning'})
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                return 200 <= reply.status < 300
        except (OSError, urllib.error.URLError) as e:
            self.log.warning("Push message failed: %s", e)
            return False

    def _password(self):
        done = subprocess.run(['security', 'find-generic-password', '-a', self.mail['user'],
                               '-s', self.mail['keychain_service'], '-w'],
                              capture_output=True, text=True, timeout=30, check=True)
        return done.stdout.strip().replace(' ', '')       # Google shows app passwords with spaces

    def _email(self, subject, body):
        m = self.mail
        if not m.get('user') or not m.get('to'):
            return None
        msg = EmailMessage()
        msg['From'], msg['To'], msg['Subject'] = m['user'], m['to'], subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(m['smtp_host'], int(m['smtp_port']), timeout=60) as smtp:
                smtp.starttls()
                smtp.login(m['user'], self._password())
                smtp.send_message(msg)
            return True
        except (OSError, smtplib.SMTPException, subprocess.SubprocessError) as e:
            self.log.warning("E-mail failed: %s", e)
            return False


class SnapshotVerifier:
    '''
    Checks a pulled evaluation snapshot directory.
    '''

    LATEST = 'LATEST.json'

    @classmethod
    def verify(cls, directory):
        '''
        :param directory: where the pull put the server's backup directory
        :type directory: Path
        :return: when the server made the newest snapshot
        :rtype: datetime
        :raises VerifyError: if it is missing, differs from its checksum or fails SQLite's check
        '''
        try:
            latest = json.loads((directory / cls.LATEST).read_text(encoding='utf-8'))
            snapshot = directory / latest['file']
            created = StatusFile.when(latest['created_utc'])
            expected = latest['sha256']
        except (OSError, ValueError, KeyError) as e:
            raise VerifyError(f"no usable {cls.LATEST}: {e}") from e
        if not snapshot.is_file():
            raise VerifyError(f"{snapshot.name} was not transferred")
        h = hashlib.sha256()
        with open(snapshot, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
        if h.hexdigest() != expected:
            raise VerifyError(f"{snapshot.name} does not match its checksum")
        try:
            db = sqlite3.connect(f"file:{snapshot}?immutable=1", uri=True)
            try:
                result = db.execute('PRAGMA integrity_check').fetchall()
            finally:
                db.close()
        except sqlite3.Error as e:
            raise VerifyError(f"{snapshot.name} cannot be opened: {e}") from e
        if result != [('ok',)]:
            raise VerifyError(f"{snapshot.name} fails SQLite's integrity check")
        return created


class BackupPuller:
    '''
    Pull, verify, record, alert.
    '''

    STATUS_NAME = 'status.json'
    LOCK_NAME = '.lock'
    LOG_NAME = 'backup_pull.log'
    LOG = logging.getLogger('backup_pull')

    def __init__(self, config, runner=None, notifier=None, clock=None):
        '''
        :param config: the settings
        :type config: PullConfig
        :param runner: does the rsync pulls
        :type runner: optional[RsyncRunner]
        :param notifier: tells the administrator
        :type notifier: optional[Notifier]
        :param clock: returns the current time as an aware datetime
        :type clock: optional[callable]
        '''
        self.config = config
        self.runner = runner or RsyncRunner(config.timeout_secs, config.rsync)
        self.notifier = notifier or Notifier(config.mail, config.ntfy)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        config.dest_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(config.dest_root, 0o700)
        self.status = StatusFile(config.dest_root / self.STATUS_NAME)

    #------------------------------------
    # run
    #-------------------

    def run(self, force=False):
        '''
        One pass over all sources.

        :param force: pull every source regardless of the retry interval
        :type force: bool
        :return: names of the sources that were pulled successfully in this pass
        :rtype: list[str]
        '''
        now = self.clock()
        pulled = []
        for source in self.config.sources:
            state = self.status.of(source['name'])
            last_ok = StatusFile.when(state.get('last_success_utc'))
            if force or last_ok is None or now - last_ok >= self.config.retry_after:
                if self._pull(source, state, now):
                    pulled.append(source['name'])
        self._alert_if_stale(now)
        self.status.save()
        return pulled

    def _pull(self, source, state, now):
        name = source['name']
        dest = self.config.dest_root / source.get('dest', name)
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        state['last_attempt_utc'] = StatusFile.text(now)
        try:
            for remote in source['remotes']:
                self.runner.pull(remote, dest, source.get('ssh_key'))
            self._lock_down(dest)
            if source.get('verify') == 'evals_snapshot':
                state['data_utc'] = StatusFile.text(SnapshotVerifier.verify(dest))
        except (RuntimeError, VerifyError) as e:
            state['last_error'] = str(e)
            self.LOG.warning("%s: %s", name, e)
            return False
        state['last_success_utc'] = StatusFile.text(now)
        state.pop('last_error', None)
        self.LOG.info("%s: pulled", name)
        return True

    @staticmethod
    def _lock_down(dest):
        for path in [dest, *dest.rglob('*')]:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)

    #------------------------------------
    # alerts
    #-------------------

    def age_of(self, name, now):
        '''
        :param name: source name
        :type name: str
        :param now: current time
        :type now: datetime
        :return: how old the newest backed-up data is: the server's snapshot time when
            known, else the last successful pull; None if never backed up
        :rtype: optional[timedelta]
        '''
        state = self.status.of(name)
        reference = StatusFile.when(state.get('data_utc')) or StatusFile.when(state.get('last_success_utc'))
        return None if reference is None else now - reference

    def _alert_if_stale(self, now):
        for source in self.config.sources:
            name = source['name']
            state = self.status.of(name)
            age = self.age_of(name, now)
            if age is not None and age <= self.config.alert_after:
                continue
            last_alert = StatusFile.when(state.get('last_alert_utc'))
            if last_alert is not None and now - last_alert < self.config.alert_repeat:
                continue
            subject, body = self._message(source, state, age)
            self.LOG.error("%s", subject)
            self.notifier.send(subject, body, short=self._short(source, subject))
            state['last_alert_utc'] = StatusFile.text(now)

    @staticmethod
    def _short(source, subject):
        '''
        :return: the text for the phone: what is wrong and what to do, nothing about hosts or errors
        :rtype: str
        '''
        lines = [subject]
        if source.get('hint'):
            lines.append(source['hint'])
        lines.append("The laptop retries hourly; details are in ~/Backups/thtrainer/backup_pull.log.")
        return ' '.join(lines)

    def _message(self, source, state, age):
        name = source['name']
        if age is None:
            subject = f"[thtrainer backup] {name} has never been backed up"
        else:
            subject = f"[thtrainer backup] {name} not backed up for {self._days(age)}"
        lines = [subject, '']
        if state.get('last_error'):
            lines += [f"The last attempt failed: {state['last_error']}", '']
        elif state.get('last_success_utc') and age is not None:
            lines += ["The last pull worked, but the data it brought is old: the process that makes "
                      "it on the other machine seems to have stopped.", '']
        if source.get('hint'):
            lines += [source['hint'], '']
        lines += [f"Last successful pull: {state.get('last_success_utc', 'never')}",
                  f"Newest data: {state.get('data_utc', state.get('last_success_utc', 'never'))}",
                  "This laptop retries hourly. To try right now:",
                  "    python -m backup_pull.backup_puller --force"]
        return subject, '\n'.join(lines) + '\n'

    @staticmethod
    def _days(age):
        hours = age.total_seconds() / 3600
        return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f} hours"

    #------------------------------------
    # report
    #-------------------

    def report(self):
        '''
        :return: one line per source: age of newest data and last error
        :rtype: str
        '''
        now = self.clock()
        out = []
        for source in self.config.sources:
            name = source['name']
            age = self.age_of(name, now)
            state = self.status.of(name)
            text = 'never backed up' if age is None else f"newest data {self._days(age)} old"
            if state.get('last_error'):
                text += f"; last error: {state['last_error']}"
            out.append(f"{name:<16} {text}")
        return '\n'.join(out)

    #------------------------------------
    # main
    #-------------------

    @classmethod
    def main(cls, argv=None):
        '''
        Command line entry.

        :param argv: arguments without program name
        :type argv: optional[list[str]]
        :return: exit code: 0 all sources fresh, 1 some stale or failing, 2 bad settings, 75 busy
        :rtype: int
        '''
        parser = argparse.ArgumentParser(description='Pull the backups and warn when they go stale.')
        parser.add_argument('--config', default=None)
        parser.add_argument('--force', action='store_true', help='pull every source now')
        parser.add_argument('--status', action='store_true', help='only show the state of each source')
        parser.add_argument('--test-alert', action='store_true', help='send a test message over every channel that is set up')
        args = parser.parse_args(argv)
        try:
            config = PullConfig.load(args.config)
        except (OSError, ValueError) as e:
            print(f"Cannot use the configuration: {e}", file=sys.stderr)
            return 2

        config.dest_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                            handlers=[logging.StreamHandler(),
                                      logging.handlers.RotatingFileHandler(config.dest_root / cls.LOG_NAME,
                                                                           maxBytes=1_000_000, backupCount=3)])
        if args.test_alert:
            result = Notifier(config.mail, config.ntfy).send_each(
                '[thtrainer backup] test', 'If you read this, backup reminders reach you.\n')
            for channel, ok in result.items():
                print(f"{channel:<13} {'not set up' if ok is None else ('sent' if ok else 'FAILED (see the log)')}")
            return 0 if any(result.values()) else 1
        lock = open(config.dest_root / cls.LOCK_NAME, 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 75
        puller = cls(config)
        if not args.status:
            puller.run(force=args.force)
        print(puller.report())
        now = puller.clock()
        stale = [s['name'] for s in config.sources
                 if puller.age_of(s['name'], now) is None or puller.age_of(s['name'], now) > config.alert_after]
        return 1 if stale else 0


if __name__ == '__main__':
    sys.exit(BackupPuller.main())
