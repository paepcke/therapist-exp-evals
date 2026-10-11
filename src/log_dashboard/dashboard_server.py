# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 09:49:30
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_dashboard/dashboard_server.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:49:30
# **********************************************************

'''
dashboard_server.py

The Tornado service behind the usage dashboard. Read-only: it reads the
de-identified logs that the trainer's server pushed, and shows aggregates
of them. It listens on a loopback address; Caddy terminates TLS in front.

    python -m log_dashboard.dashboard_server            # settings: see --help

Every route except /healthz needs the login from the password file.
'''

import asyncio
import base64
import binascii
import json
import logging
import sys
from pathlib import Path

import tornado.web
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop

from log_dashboard.dashboard_config import ConfigError, DashboardConfig
from log_dashboard.dashboard_data import DashboardData, IdentifyingDataError, ParticipantsUnavailable
from log_dashboard.password_file import PasswordFile


class SecurityMixin:
    '''
    Login check and protective headers for every handler.
    '''

    REALM = 'Experiment dashboard'
    FAILED_LOGIN_DELAY_SECS = 1.0

    # Nothing is loaded from anywhere but this server:
    CONTENT_SECURITY_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'")
    # Subclasses that need the login check set this to False (health check):
    REQUIRES_LOGIN = True

    def set_default_headers(self):
        self.set_header('Content-Security-Policy', self.CONTENT_SECURITY_POLICY)
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('X-Frame-Options', 'DENY')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.set_header('Cache-Control', 'no-store')
        self.clear_header('Server')

    async def prepare(self):
        if not self.REQUIRES_LOGIN:
            return
        passwords = self.application.settings.get('passwords')
        if passwords is None:                  # --no-auth, loopback only
            return
        if self._login_ok(passwords):
            return
        await asyncio.sleep(self.FAILED_LOGIN_DELAY_SECS)
        self.set_status(401)
        self.set_header('WWW-Authenticate', f'Basic realm="{self.REALM}", charset="UTF-8"')
        self.finish('Login required')

    def _login_ok(self, passwords):
        header = self.request.headers.get('Authorization', '')
        scheme, _, encoded = header.partition(' ')
        if scheme.lower() != 'basic' or not encoded:
            return False
        try:
            user, sep, password = base64.b64decode(encoded, validate=True).decode('utf-8').partition(':')
        except (binascii.Error, UnicodeDecodeError):
            return False
        return bool(sep) and passwords.verify(user, password)


class IndexHandler(SecurityMixin, tornado.web.RequestHandler):
    '''
    The page itself.
    '''

    def get(self):
        self.set_header('Content-Type', 'text/html; charset=UTF-8')
        self.write((self.application.settings['static_dir'] / 'index.html').read_bytes())


class StaticHandler(SecurityMixin, tornado.web.StaticFileHandler):
    '''
    The page's script, style and d3 library.
    '''

    def set_extra_headers(self, path):
        # Allow reuse by the browser, but only after it has been logged in:
        self.set_header('Cache-Control', 'private, max-age=300')


class DataHandler(SecurityMixin, tornado.web.RequestHandler):
    '''
    GET /api/data[?participants_only=1] -> the dashboard document.
    '''

    TRUE_VALUES = ('1', 'true', 'yes', 'on')

    async def get(self):
        only = self.get_query_argument('participants_only', '0').lower() in self.TRUE_VALUES
        data = self.application.settings['data']
        try:
            document = await IOLoop.current().run_in_executor(None, data.document, only)
        except ParticipantsUnavailable as e:
            return self._error(409, str(e))
        except IdentifyingDataError as e:
            logging.getLogger('log_dashboard').error("%s", e)
            return self._error(500, str(e))
        except FileNotFoundError as e:
            return self._error(503, f"Log directory is not readable: {e}")
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        self.finish(json.dumps(document, allow_nan=False))

    def _error(self, status, message):
        self.set_status(status)
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        self.finish(json.dumps({'error': message}))


class HealthHandler(SecurityMixin, tornado.web.RequestHandler):
    '''
    GET /healthz -> ok. No login, no data.
    '''

    REQUIRES_LOGIN = False

    def get(self):
        self.set_header('Content-Type', 'text/plain; charset=UTF-8')
        self.finish('ok')


class DashboardServer:
    '''
    Builds the application and runs it.
    '''

    STATIC_DIR = Path(__file__).resolve().parent / 'static'
    STATIC_URL_PREFIX = '/static/'

    LOG = logging.getLogger('log_dashboard')

    # ------------------------- Constructor ---------------------

    def __init__(self, config):
        '''
        :param config: settings
        :type config: DashboardConfig
        '''
        self._config = config

    # ------------------------- Public API ----------------------

    def make_app(self):
        '''
        :return: the Tornado application
        :rtype: tornado.web.Application
        '''
        passwords = None if self._config.no_auth else PasswordFile.load(self._config.auth_file)
        return tornado.web.Application(
            [
                (r'/', IndexHandler),
                (r'/api/data', DataHandler),
                (r'/healthz', HealthHandler),
                (r'/static/(.*)', StaticHandler, {'path': str(self.STATIC_DIR)}),
            ],
            data=DashboardData(self._config),
            passwords=passwords,
            static_dir=self.STATIC_DIR,
            )

    def serve(self):
        '''
        Runs until stopped.
        '''
        async def run():
            server = HTTPServer(self.make_app())
            server.listen(self._config.port, address=self._config.bind)
            self.LOG.info("Serving %s on %s:%s%s", self._config.log_dir, self._config.bind, self._config.port,
                          ' WITHOUT LOGIN' if self._config.no_auth else '')
            await asyncio.Event().wait()

        asyncio.run(run())

    @classmethod
    def main(cls, argv=None, environ=None):
        '''
        :param argv: arguments without program name
        :type argv: union[list[str], None]
        :param environ: environment; default os.environ
        :type environ: union[dict, None]
        :return: exit code
        :rtype: int
        '''
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(levelname)s %(message)s')
        try:
            config = DashboardConfig.from_cli(argv, environ)
            server = cls(config)
            server.make_app()               # fail now on a bad password file
        except (ConfigError, ValueError) as e:
            cls.LOG.error("%s", e)
            return 3
        try:
            server.serve()
        except KeyboardInterrupt:
            pass
        return 0


if __name__ == '__main__':
    sys.exit(DashboardServer.main())
