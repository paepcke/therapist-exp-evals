# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/evals_admin.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
evals_admin.py

The administrator's command line: create accounts and invite codes, load the
roster's people, list accounts, disable one. Run it as the service's own
account so that the database stays owned by it:

    sudo -u evals <python> -m evals_service.evals_admin list
    sudo -u evals <python> -m evals_service.evals_admin invite --email jon@example.org \\
         --name "Jon Frederickson" --roles evaluator --label Evaluator_1
    sudo -u evals <python> -m evals_service.evals_admin invite --email me@example.org --roles admin,evaluator
    sudo -u evals <python> -m evals_service.evals_admin import-roster
    sudo -u evals <python> -m evals_service.evals_admin invite --email jon@example.org --reissue

An invite code is shown once; only its hash is stored. Give it to the person
by a channel you trust. They enter it, with their e-mail address, on the
"First visit" page and choose their own password. After that, their browser
remembers them.
'''

import argparse
import sys
from pathlib import Path

from evals_service.credentials import Credentials
from evals_service.eval_store import EvalStore, StoreError
from evals_service.evals_common import EvalsCommon
from evals_service.roster import Roster


class EvalsAdmin:
    '''
    Subcommands of the administrator's tool.
    '''

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, service_root=None, out=None):
        '''
        :param service_root: the service's directory
        :type service_root: optional[union[str, Path]]
        :param out: where to print; defaults to stdout
        :type out: optional[file-like]
        '''
        self.root = Path(service_root or EvalsCommon.SERVICE_ROOT)
        self.out = out or sys.stdout
        self.store = EvalStore(self.root / EvalsCommon.DATA_DIR_NAME / EvalsCommon.DB_NAME)

    def _say(self, text=''):
        print(text, file=self.out)

    #------------------------------------
    # invite
    #-------------------

    def invite(self, email, roles, name=None, label=None, reissue=False):
        '''
        Create an account with an invite code, or issue a new code for an account
        that has not been activated (or with reissue=True, for one that has: the
        old password stays valid until the new code is used).

        :param email: login name
        :type email: str
        :param roles: list of roles
        :type roles: list[str]
        :param name: real name
        :type name: optional[str]
        :param label: display label; default Evaluator_<n> for evaluators
        :type label: optional[str]
        :param reissue: allow a new code for an activated account
        :type reissue: bool
        :return: the code, to be handed to the person
        :rtype: str
        :raises StoreError: if the account exists and reissue was not asked for
        '''
        email = Credentials.normalize_email(email)
        code = Credentials.new_invite_code()
        person = self.store.person_by_email(email)
        if person is None:
            self.store.add_person(email, roles, name=name, label=label, invite_hash=Credentials.hash_code(code))
        elif person['activated_utc'] is not None and not reissue:
            raise StoreError(f"{email} already has a password. Use --reissue to give a new code.")
        else:
            self.store.set_invite(person['id'], Credentials.hash_code(code))
        return code

    #------------------------------------
    # import_roster
    #-------------------

    def import_roster(self):
        '''
        Create an account for everybody in roster.json who has none. Instructors become
        evaluators (Evaluator_<n> in order of appearance); students get the student role.

        :return: one dict per new account: email, name, label, roles, code
        :rtype: list[dict]
        '''
        roster = Roster.load(self.root / EvalsCommon.CONFIG_DIR_NAME / EvalsCommon.ROSTER_NAME)
        created = []
        for email, name, kind in roster.all_emails():
            if self.store.person_by_email(email) is not None:
                continue
            role = EvalsCommon.ROLE_EVALUATOR if kind == 'instructor' else EvalsCommon.ROLE_STUDENT
            code = Credentials.new_invite_code()
            self.store.add_person(email, [role], name=name, invite_hash=Credentials.hash_code(code))
            person = self.store.person_by_email(email)
            created.append({'email': email, 'name': name, 'label': person['label'], 'roles': role, 'code': code})
        return created

    #------------------------------------
    # listing
    #-------------------

    def list_people(self):
        '''
        Print one line per account.
        '''
        for p in self.store.all_people():
            state = 'disabled' if p['disabled'] else ('active' if p['activated_utc'] else 'invited')
            self._say(f"{p['id']:>3}  {p['label']:<14} {p['email']:<34} {p['roles']:<18} {state:<9} "
                      f"last seen {p['last_seen_utc'] or '-'}")

    #------------------------------------
    # set_disabled
    #-------------------

    def set_disabled(self, email, disabled):
        '''
        :param email: account
        :type email: str
        :param disabled: True to lock the account and log out its browsers
        :type disabled: bool
        :raises StoreError: if there is no such account
        '''
        person = self.store.person_by_email(Credentials.normalize_email(email))
        if person is None:
            raise StoreError("no such account")
        self.store.set_disabled(person['id'], disabled)
        if disabled:
            self.store.drop_all_device_tokens(person['id'])

    #------------------------------------
    # main
    #-------------------

    @classmethod
    def main(cls, argv=None, out=None):
        '''
        Command line entry.

        :param argv: arguments without program name
        :type argv: optional[list[str]]
        :param out: where to print
        :type out: optional[file-like]
        :return: exit code
        :rtype: int
        '''
        parser = argparse.ArgumentParser(description='Administer the evaluation service.')
        parser.add_argument('--service-root', default=str(EvalsCommon.SERVICE_ROOT))
        sub = parser.add_subparsers(dest='command', required=True)
        p = sub.add_parser('invite', help='create an account, or give a new invite code')
        p.add_argument('--email', required=True)
        p.add_argument('--roles', default=EvalsCommon.ROLE_EVALUATOR, help='comma separated: admin,evaluator,student')
        p.add_argument('--name')
        p.add_argument('--label', help='default Evaluator_<n> for evaluators')
        p.add_argument('--reissue', action='store_true')
        sub.add_parser('import-roster', help='create accounts for everybody in roster.json')
        sub.add_parser('list', help='list accounts')
        for name in ('disable', 'enable'):
            sub.add_parser(name, help=f'{name} an account').add_argument('--email', required=True)
        args = parser.parse_args(argv)

        admin = cls(args.service_root, out)
        try:
            if args.command == 'invite':
                code = admin.invite(args.email, [r.strip() for r in args.roles.split(',') if r.strip()],
                                    args.name, args.label, args.reissue)
                admin._say(f"Invite code for {args.email}: {code}")
                admin._say("It is shown only now. It is valid for "
                           f"{EvalsCommon.INVITE_VALID_DAYS} days.")
            elif args.command == 'import-roster':
                created = admin.import_roster()
                admin._say('email,name,label,roles,invite_code')
                for c in created:
                    admin._say(f"{c['email']},\"{c['name']}\",{c['label']},{c['roles']},{c['code']}")
                admin._say(f"# {len(created)} new account(s). The codes are shown only now.")
            elif args.command == 'list':
                admin.list_people()
            else:
                admin.set_disabled(args.email, args.command == 'disable')
                admin._say(f"{args.command}d {args.email}")
        except (StoreError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0


if __name__ == '__main__':
    sys.exit(EvalsAdmin.main())
