# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 09:48:46
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/log_dashboard/password_file.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 09:48:46
# **********************************************************

'''
password_file.py

The dashboard's single login, stored as a salted scrypt hash in a file
that only the dashboard's account can read. Create or change it with:

    python -m log_dashboard.password_file --file /srv/logdash/auth.txt --user <name>

The file holds one line: user$salt$hash (hex). The password is asked for
twice and is never echoed or stored.
'''

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path


class PasswordFile:
    '''
    One user name and password hash.
    '''

    SEPARATOR = '$'
    SALT_BYTES = 16
    # scrypt cost: about 16 MiB and 50 ms; raise N for a stronger hash
    SCRYPT_N = 2 ** 14
    SCRYPT_R = 8
    SCRYPT_P = 1
    HASH_BYTES = 32
    MIN_PASSWORD_LEN = 12
    FILE_MODE = 0o600

    # ------------------------- Constructor ---------------------

    def __init__(self, user, salt, digest):
        '''
        :param user: login name
        :type user: str
        :param salt: random salt
        :type salt: bytes
        :param digest: scrypt hash of the password
        :type digest: bytes
        '''
        self._user = user
        self._salt = salt
        self._digest = digest

    # ------------------------- Public API ----------------------

    @classmethod
    def create(cls, user, password):
        '''
        :param user: login name; no '$' or whitespace
        :type user: str
        :param password: the new password
        :type password: str
        :return: the entry
        :rtype: PasswordFile
        :raises ValueError: for a bad user name or a short password
        '''
        if not user or cls.SEPARATOR in user or user != user.strip() or any(c.isspace() for c in user):
            raise ValueError("User name must be non-empty, without spaces or '$'")
        if len(password) < cls.MIN_PASSWORD_LEN:
            raise ValueError(f"Password must have at least {cls.MIN_PASSWORD_LEN} characters")
        salt = secrets.token_bytes(cls.SALT_BYTES)
        return cls(user, salt, cls._hash(password, salt))

    @classmethod
    def load(cls, path):
        '''
        :param path: file written by save()
        :type path: union[str, Path]
        :return: the entry
        :rtype: PasswordFile
        :raises ValueError: if the file is not in the expected format
        '''
        parts = Path(path).read_text(encoding='utf-8').strip().split(cls.SEPARATOR)
        if len(parts) != 3:
            raise ValueError(f"{path} is not a dashboard password file")
        user, salt_hex, digest_hex = parts
        try:
            return cls(user, bytes.fromhex(salt_hex), bytes.fromhex(digest_hex))
        except ValueError as e:
            raise ValueError(f"{path} is not a dashboard password file") from e

    def save(self, path):
        '''
        Writes the file with mode 600, replacing any earlier one.

        :param path: destination
        :type path: union[str, Path]
        '''
        path = Path(path)
        line = self.SEPARATOR.join((self._user, self._salt.hex(), self._digest.hex())) + '\n'
        temp = path.with_name('.' + path.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self.FILE_MODE)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(line)
        os.replace(temp, path)

    def verify(self, user, password):
        '''
        Constant-time check of both parts, so that timing does not say
        which of them was wrong.

        :param user: offered login name
        :type user: str
        :param password: offered password
        :type password: str
        :return: True if both are right
        :rtype: bool
        '''
        same_digest = hmac.compare_digest(self._hash(password, self._salt), self._digest)
        same_user = hmac.compare_digest(user.encode('utf-8'), self._user.encode('utf-8'))
        return same_digest and same_user

    @classmethod
    def main(cls, argv=None):
        '''
        Command line entry: asks for a password and writes the file.

        :param argv: arguments without program name
        :type argv: union[list[str], None]
        :return: exit code
        :rtype: int
        '''
        parser = argparse.ArgumentParser(description='Create the dashboard password file.')
        parser.add_argument('--file', required=True)
        parser.add_argument('--user', required=True)
        args = parser.parse_args(argv)
        first = getpass.getpass('New dashboard password: ')
        if first != getpass.getpass('Again: '):
            print('The two entries differ. Nothing written.', file=sys.stderr)
            return 1
        try:
            cls.create(args.user, first).save(args.file)
        except ValueError as e:
            print(f"{e}. Nothing written.", file=sys.stderr)
            return 1
        print(f"Wrote {args.file}. Give it to the dashboard's account: chown <account> {args.file}")
        return 0

    # ------------------------- Private -------------------------

    @classmethod
    def _hash(cls, password, salt):
        return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=cls.SCRYPT_N, r=cls.SCRYPT_R,
                              p=cls.SCRYPT_P, dklen=cls.HASH_BYTES)


if __name__ == '__main__':
    sys.exit(PasswordFile.main())
