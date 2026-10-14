# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/evals_server.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
evals_server.py

The Tornado service behind the evaluation forms. It serves one static page
(static/index.html with ES modules) and a small JSON API:

    POST /api/register          e-mail, invite code, new password   -> device token
    POST /api/login             e-mail, password                    -> device token
    POST /api/logout
    GET  /api/me                who am I, and what is waiting
    GET  /api/review/next       id of the next session to review
    GET  /api/item              a form with its subject and the saved answers
    PUT  /api/rating            save a draft
    POST /api/rating/submit     submit; final
    GET  /api/admin/progress    progress of everyone (administrator only)
    GET  /api/admin/export.csv  all submitted ratings (administrator only)
    GET  /healthz               'ok', no login

The browser keeps the device token in localStorage and sends it as
'Authorization: Bearer <token>'. A token is valid for months of use, so that
people come back without typing a password. The page is served with a
Content-Security-Policy that allows scripts and styles only from this server.

Run behind a reverse proxy that terminates TLS:

    python -m evals_service.evals_server [--service-root /srv/evals] [--port 8082]
'''

import argparse
import asyncio
import csv
import io
import json
import logging
import mimetypes
import sys
import time
from pathlib import Path

import tornado.httpserver
import tornado.web
from tornado.ioloop import IOLoop

from evals_service.bundle_store import BundleStore
from evals_service.credentials import Credentials
from evals_service.eval_store import EvalStore, StoreError
from evals_service.evals_common import EvalsCommon
from evals_service.roster import Roster
from evals_service.rubrics import RubricLibrary
from evals_service.workflow import AccessError, Workflow


class LoginThrottle:
    '''
    Remembers failed logins per (client address, e-mail) and refuses further
    attempts for a while. Kept in memory; a restart forgets, which is fine.
    '''

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._failures = {}

    def blocked(self, key):
        '''
        :param key: (address, e-mail)
        :type key: tuple
        :return: True if too many recent failures
        :rtype: bool
        '''
        count, since = self._failures.get(key, (0, 0))
        if count >= EvalsCommon.LOGIN_MAX_FAILURES:
            if self._clock() - since < EvalsCommon.LOGIN_LOCK_SECS:
                return True
            del self._failures[key]
        return False

    def failed(self, key):
        count, _ = self._failures.get(key, (0, 0))
        self._failures[key] = (count + 1, self._clock())

    def succeeded(self, key):
        self._failures.pop(key, None)


class ApiHandler(tornado.web.RequestHandler):
    '''
    Common behavior: protective headers, JSON in and out, the device-token login.
    '''

    CONTENT_SECURITY_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    MAX_JSON_BYTES = 200_000
    REQUIRES_LOGIN = True
    REQUIRED_ROLE = None

    def set_default_headers(self):
        self.set_header('Content-Security-Policy', self.CONTENT_SECURITY_POLICY)
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('X-Frame-Options', 'DENY')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.set_header('Cache-Control', 'no-store')
        self.clear_header('Server')

    @property
    def ctx(self):
        return self.application.settings['ctx']

    def prepare(self):
        self.person = None
        if not self.REQUIRES_LOGIN:
            return
        scheme, _, token = self.request.headers.get('Authorization', '').partition(' ')
        if scheme.lower() == 'bearer' and token:
            self.person = self.ctx.store.person_for_token(Credentials.hash_token(token.strip()))
        if self.person is None:
            self.fail(401, "Please log in.")
        if self.REQUIRED_ROLE and self.REQUIRED_ROLE not in self.person['roles'].split(','):
            self.fail(403, "Your account may not do that.")

    def fail(self, status, message):
        '''
        Answer with a JSON error and stop.

        :param status: HTTP status
        :type status: int
        :param message: text for the person
        :type message: str
        '''
        self.set_status(status)
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        self.finish(json.dumps({'error': message}))
        raise tornado.web.Finish()

    def reply(self, document):
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        self.finish(json.dumps(document, allow_nan=False))

    def body(self):
        '''
        :return: the parsed JSON request body, which must be an object
        :rtype: dict
        '''
        if len(self.request.body) > self.MAX_JSON_BYTES:
            self.fail(413, "That is too much text.")
        try:
            doc = json.loads(self.request.body.decode('utf-8'))
        except ValueError:
            self.fail(400, "The request was not valid JSON.")
        if not isinstance(doc, dict):
            self.fail(400, "The request must be a JSON object.")
        return doc

    def client_key(self, email):
        return (self.request.remote_ip, email)

    def me(self, person):
        roles = person['roles'].split(',')
        return {'label': person['label'], 'name': person['name'], 'email': person['email'], 'roles': roles,
                'is_admin': EvalsCommon.ROLE_ADMIN in roles}

    def write_error(self, status_code, **kwargs):
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        if status_code >= 500:
            logging.getLogger('evals_service').error("Unhandled error", exc_info=kwargs.get('exc_info'))
            self.finish(json.dumps({'error': "Something went wrong on the server."}))
        else:
            self.finish(json.dumps({'error': self._reason}))


class IndexHandler(ApiHandler):
    REQUIRES_LOGIN = False

    def get(self):
        self.set_header('Content-Type', 'text/html; charset=UTF-8')
        self.write((self.application.settings['static_dir'] / 'index.html').read_bytes())


class StaticHandler(tornado.web.StaticFileHandler):
    '''
    The page's scripts and styles. Not secret, so no login is needed to fetch them.
    '''

    def set_default_headers(self):
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.clear_header('Server')

    def set_extra_headers(self, path):
        self.set_header('Cache-Control', 'no-cache')

    def get_content_type(self):
        if self.absolute_path.endswith('.mjs'):
            return 'text/javascript'
        return super().get_content_type()


class HealthHandler(ApiHandler):
    REQUIRES_LOGIN = False

    def get(self):
        self.set_header('Content-Type', 'text/plain; charset=UTF-8')
        self.finish('ok')


class RegisterHandler(ApiHandler):
    '''
    First visit: the invite code, which the administrator hands out, lets the person choose a password.
    '''
    REQUIRES_LOGIN = False
    GENERIC = "That e-mail address and invite code do not match."

    async def post(self):
        doc = self.body()
        try:
            email = Credentials.normalize_email(doc.get('email'))
            Credentials.check_password_policy(doc.get('password'))
        except ValueError as e:
            return self.fail(400, str(e))
        key = self.client_key(email)
        if self.ctx.throttle.blocked(key):
            return self.fail(429, "Too many tries. Please wait a few minutes.")
        person = self.ctx.store.person_by_email(email)
        code_hash = Credentials.hash_code(str(doc.get('code') or ''))
        now_iso = self.ctx.store.now_iso()
        if (person is None or person['disabled'] or person['invite_hash'] is None
                or person['invite_hash'] != code_hash or (person['invite_expires_utc'] or '') <= now_iso):
            self.ctx.throttle.failed(key)
            await asyncio.sleep(0.5)
            return self.fail(403, self.GENERIC)
        pw_hash = await IOLoop.current().run_in_executor(None, Credentials.hash_password, doc['password'])
        self.ctx.throttle.succeeded(key)
        self.ctx.store.activate(person['id'], pw_hash)
        self.ctx.store.drop_all_device_tokens(person['id'])
        self.reply(self.ctx.issue_session(person['id'], self.me(self.ctx.store.person_by_id(person['id']))))


class LoginHandler(ApiHandler):
    REQUIRES_LOGIN = False
    GENERIC = "That e-mail address and password do not match."

    async def post(self):
        doc = self.body()
        try:
            email = Credentials.normalize_email(doc.get('email'))
        except ValueError:
            return self.fail(403, self.GENERIC)
        key = self.client_key(email)
        if self.ctx.throttle.blocked(key):
            return self.fail(429, "Too many tries. Please wait a few minutes.")
        person = self.ctx.store.person_by_email(email)
        stored = person['pw_hash'] if person else None
        ok = await IOLoop.current().run_in_executor(
            None, Credentials.verify_password, str(doc.get('password') or ''), stored)
        if not ok or person['disabled']:
            self.ctx.throttle.failed(key)
            return self.fail(403, self.GENERIC)
        self.ctx.throttle.succeeded(key)
        self.ctx.store.touch(person['id'])
        self.reply(self.ctx.issue_session(person['id'], self.me(person)))


class LogoutHandler(ApiHandler):
    def post(self):
        _, _, token = self.request.headers.get('Authorization', '').partition(' ')
        self.ctx.store.drop_device_token(Credentials.hash_token(token.strip()))
        self.reply({'ok': True})


class MeHandler(ApiHandler):
    def get(self):
        self.ctx.refresh()
        self.reply({'me': self.me(self.person), 'overview': self.ctx.workflow.overview(self.person),
                    'bundle': self.ctx.bundles.info()})


class NextReviewHandler(ApiHandler):
    REQUIRED_ROLE = EvalsCommon.ROLE_EVALUATOR

    def get(self):
        self.ctx.refresh()
        try:
            self.reply({'subject': self.ctx.workflow.next_review(self.person)})
        except AccessError as e:
            self.fail(403, str(e))


class ItemHandler(ApiHandler):
    '''
    GET /api/item?form=<kind>&subject=<key>
    '''

    def get(self):
        self.ctx.refresh()
        kind = self.get_query_argument('form', '')
        subject = self.get_query_argument('subject', '')
        try:
            info = self.ctx.workflow.resolve(self.person, kind, subject)
        except AccessError as e:
            return self.fail(404, str(e))
        self.ctx.store.mark_seen(self.person['id'], kind, subject)
        rating = self.ctx.store.get_rating(self.person['id'], kind, subject)
        self.reply({'form': self.ctx.rubrics.form_for_display(kind, defense=info['defense']),
                    'subject': info['subject'], 'key': subject,
                    'rating': ({'state': rating['state'], 'answers': rating['answers'],
                                'updated_utc': rating['updated_utc']} if rating else None)})


class RatingHandler(ApiHandler):
    '''
    PUT saves a draft; POST (at /api/rating/submit) submits.
    '''
    SUBMIT = False

    def _save(self):
        doc = self.body()
        kind, subject = str(doc.get('form', '')), str(doc.get('subject', ''))
        try:
            self.ctx.workflow.resolve(self.person, kind, subject)
            answers = self.ctx.rubrics.clean_answers(kind, doc.get('answers'), complete=self.SUBMIT)
            write = self.ctx.store.submit if self.SUBMIT else self.ctx.store.save_draft
            row = write(self.person['id'], kind, subject, answers, self.ctx.rubrics.version)
        except AccessError as e:
            return self.fail(404, str(e))
        except ValueError as e:
            return self.fail(400, str(e))
        except StoreError as e:
            return self.fail(409, str(e))
        self.reply({'state': row['state'], 'updated_utc': row['updated_utc']})

    def put(self):
        if self.SUBMIT:
            return self.fail(405, "Use POST.")
        self._save()

    def post(self):
        if not self.SUBMIT:
            return self.fail(405, "Use PUT.")
        self._save()


class SubmitHandler(RatingHandler):
    SUBMIT = True


class ProgressHandler(ApiHandler):
    REQUIRED_ROLE = EvalsCommon.ROLE_ADMIN

    def get(self):
        self.ctx.refresh()
        self.reply(self.ctx.workflow.progress())


class ExportHandler(ApiHandler):
    '''
    One CSV row per submitted rating item. For the administrator's analysis.
    '''
    REQUIRED_ROLE = EvalsCommon.ROLE_ADMIN

    def get(self):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['rater', 'rater_email', 'form', 'subject', 'section', 'item', 'value',
                         'submitted_utc', 'rubric_version'])
        for r in self.ctx.store.export_submitted():
            for section, items in r['answers'].items():
                for item, value in items.items():
                    writer.writerow([r['rater_label'], r['rater_email'], r['form_kind'], r['subject_key'],
                                     section, item, value, r['submitted_utc'], r['rubric_version']])
        self.set_header('Content-Type', 'text/csv; charset=UTF-8')
        self.set_header('Content-Disposition', 'attachment; filename="ratings.csv"')
        self.finish(out.getvalue())


class ServiceContext:
    '''
    The objects the handlers share.
    '''

    def __init__(self, service_root=None, inbox_dir=None, rubrics_path=None, clock=None):
        '''
        :param service_root: directory with data/, config/, releases/
        :type service_root: optional[union[str, Path]]
        :param inbox_dir: where bundles arrive
        :type inbox_dir: optional[union[str, Path]]
        :param rubrics_path: alternative rubrics.json
        :type rubrics_path: optional[union[str, Path]]
        :param clock: aware-datetime clock, for tests
        :type clock: optional[callable]
        '''
        self.root = Path(service_root or EvalsCommon.SERVICE_ROOT)
        self.store = EvalStore(self.root / EvalsCommon.DATA_DIR_NAME / EvalsCommon.DB_NAME, clock=clock)
        self.rubrics = RubricLibrary(rubrics_path)
        self.bundles = BundleStore(inbox_dir, self.root)
        self._roster_path = self.root / EvalsCommon.CONFIG_DIR_NAME / EvalsCommon.ROSTER_NAME
        self._roster_stamp = None
        self.workflow = Workflow(self.store, Roster(), self.bundles, self.rubrics, clock=clock)
        self.throttle = LoginThrottle()
        self.refresh()

    def refresh(self):
        '''
        Pick up a new bundle or an edited roster.
        '''
        self.bundles.refresh()
        try:
            stamp = self._roster_path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self._roster_stamp:
            try:
                self.workflow.set_roster(Roster.load(self._roster_path))
                self._roster_stamp = stamp
            except (ValueError, KeyError, OSError) as e:
                self._roster_stamp = stamp
                logging.getLogger('evals_service').error("Roster not loaded: %s", e)

    def issue_session(self, person_id, me):
        '''
        :param person_id: who logged in
        :type person_id: int
        :param me: the person's public data
        :type me: dict
        :return: {'token': new device token, 'me': me}
        :rtype: dict
        '''
        token, token_hash = Credentials.new_device_token()
        self.store.add_device_token(person_id, token_hash)
        return {'token': token, 'me': me}


class EvalsServer:
    '''
    Builds and runs the Tornado application.
    '''

    STATIC_DIR = Path(__file__).parent / 'static'

    #------------------------------------
    # make_app
    #-------------------

    @classmethod
    def make_app(cls, ctx):
        '''
        :param ctx: shared objects
        :type ctx: ServiceContext
        :return: the application
        :rtype: tornado.web.Application
        '''
        mimetypes.add_type('text/javascript', '.mjs')
        return tornado.web.Application(
            [
                (r'/', IndexHandler),
                (r'/healthz', HealthHandler),
                (r'/api/register', RegisterHandler),
                (r'/api/login', LoginHandler),
                (r'/api/logout', LogoutHandler),
                (r'/api/me', MeHandler),
                (r'/api/review/next', NextReviewHandler),
                (r'/api/item', ItemHandler),
                (r'/api/rating', RatingHandler),
                (r'/api/rating/submit', SubmitHandler),
                (r'/api/admin/progress', ProgressHandler),
                (r'/api/admin/export\.csv', ExportHandler),
                (r'/static/(.*)', StaticHandler, {'path': str(cls.STATIC_DIR)}),
            ],
            ctx=ctx, static_dir=cls.STATIC_DIR)

    #------------------------------------
    # main
    #-------------------

    @classmethod
    def main(cls, argv=None):
        '''
        Command line entry.

        :param argv: arguments without program name
        :type argv: optional[list[str]]
        :return: exit code
        :rtype: int
        '''
        parser = argparse.ArgumentParser(description='Evaluation forms service.')
        parser.add_argument('--service-root', default=str(EvalsCommon.SERVICE_ROOT))
        parser.add_argument('--inbox', default=str(EvalsCommon.INBOX_DIR))
        parser.add_argument('--bind', default=EvalsCommon.DEFAULT_BIND)
        parser.add_argument('--port', type=int, default=EvalsCommon.DEFAULT_PORT)
        args = parser.parse_args(argv)
        logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
        try:
            ctx = ServiceContext(args.service_root, args.inbox)
        except (OSError, ValueError) as e:
            logging.getLogger('evals_service').error("Cannot start: %s", e)
            return 3
        app = cls.make_app(ctx)
        server = tornado.httpserver.HTTPServer(app, xheaders=True, max_body_size=1_000_000)
        server.listen(args.port, address=args.bind)
        logging.getLogger('evals_service').info("Listening on %s:%s", args.bind, args.port)
        IOLoop.current().start()
        return 0


if __name__ == '__main__':
    sys.exit(EvalsServer.main())
