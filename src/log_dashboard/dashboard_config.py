# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 09:48:46
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_dashboard/dashboard_config.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:48:46
# **********************************************************

'''
dashboard_config.py

Settings of the usage dashboard service. Each has a default, an
environment variable (THDASH_<NAME>), and a command line option; the command
line wins over the environment, which wins over the default.
'''

import argparse
import os
from pathlib import Path


class ConfigError(ValueError):
    '''
    Raised for settings that cannot work.
    '''


class DashboardConfig:
    '''
    Where the logs are, where to listen, and how to authenticate.
    '''

    ENV_PREFIX = 'THDASH_'

    # The cloud machine's inbox for logs, readable by the dashboard's account:
    DEFAULT_LOG_DIR = Path('/srv/thdata/inbox/logs')
    DEFAULT_AUTH_FILE = Path('/srv/logdash/auth.txt')

    # The web server in front (Caddy) terminates TLS and proxies to this:
    DEFAULT_BIND = '127.0.0.1'
    DEFAULT_PORT = 8081
    LOOPBACK_ADDRESSES = ('127.0.0.1', '::1', 'localhost')

    PARTICIPANTS_FILE_NAME = 'participants.json'

    # The first this-many cases a participant opens are practice cases.
    # Must equal EvalsCommon.TRIAL_CASE_COUNT in the therapist repository.
    DEFAULT_TRIAL_CASE_COUNT = 4

    # ------------------------- Constructor ---------------------

    def __init__(self, log_dir=None, bind=None, port=None, auth_file=None, no_auth=False,
                 trial_case_count=None):
        '''
        :param log_dir: directory with the de-identified logs
        :type log_dir: union[str, Path, None]
        :param bind: address to listen on
        :type bind: union[str, None]
        :param port: port to listen on
        :type port: union[int, None]
        :param auth_file: file with the dashboard's password hash
        :type auth_file: union[str, Path, None]
        :param no_auth: serve without a password. Only on a loopback address.
        :type no_auth: bool
        :param trial_case_count: number of practice cases per participant
        :type trial_case_count: union[int, None]
        :raises ConfigError: for a missing log directory or password file,
            or for no_auth on a non-loopback address
        '''
        self.log_dir = Path(log_dir) if log_dir else self.DEFAULT_LOG_DIR
        self.bind = bind or self.DEFAULT_BIND
        self.port = int(port) if port else self.DEFAULT_PORT
        self.auth_file = Path(auth_file) if auth_file else self.DEFAULT_AUTH_FILE
        self.no_auth = bool(no_auth)
        self.trial_case_count = (int(trial_case_count) if trial_case_count is not None
                                 else self.DEFAULT_TRIAL_CASE_COUNT)
        self._validate()

    @property
    def participants_path(self):
        '''
        :return: where participants.json is expected
        :rtype: Path
        '''
        return self.log_dir / self.PARTICIPANTS_FILE_NAME

    @classmethod
    def from_cli(cls, argv=None, environ=None):
        '''
        :param argv: command line arguments, without the program name
        :type argv: union[list[str], None]
        :param environ: environment; default os.environ
        :type environ: union[dict, None]
        :return: the settings
        :rtype: DashboardConfig
        :raises ConfigError: for unusable settings
        '''
        env = os.environ if environ is None else environ

        def default(name):
            return env.get(cls.ENV_PREFIX + name) or None

        parser = argparse.ArgumentParser(
            description='Serve the experiment usage dashboard.',
            epilog='Each option can also be set with the environment variable THDASH_<OPTION>.')
        parser.add_argument('--log-dir', default=default('LOG_DIR'), help=f'default {cls.DEFAULT_LOG_DIR}')
        parser.add_argument('--bind', default=default('BIND'), help=f'default {cls.DEFAULT_BIND}')
        parser.add_argument('--port', default=default('PORT'), type=int, help=f'default {cls.DEFAULT_PORT}')
        parser.add_argument('--auth-file', default=default('AUTH_FILE'), help=f'default {cls.DEFAULT_AUTH_FILE}')
        parser.add_argument('--trial-case-count', default=default('TRIAL_CASE_COUNT'), type=int,
                            help=f'practice cases per participant; default {cls.DEFAULT_TRIAL_CASE_COUNT}')
        parser.add_argument('--no-auth', action='store_true', default=bool(default('NO_AUTH')),
                            help='no password; only on a loopback address (development)')
        return cls(**vars(parser.parse_args(argv)))

    # ------------------------- Private -------------------------

    def _validate(self):
        if not self.log_dir.is_dir():
            raise ConfigError(f"Log directory does not exist: {self.log_dir}")
        if self.no_auth:
            if self.bind not in self.LOOPBACK_ADDRESSES:
                raise ConfigError("--no-auth is allowed only on a loopback address")
        elif not self.auth_file.is_file():
            raise ConfigError(f"Password file not found: {self.auth_file}. Create it with: "
                              f"python -m log_dashboard.password_file --file {self.auth_file} --user <name>")
