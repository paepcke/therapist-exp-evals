# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/credentials.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
credentials.py

Password hashing, invite codes and device tokens. Nothing here touches the
database; the store keeps the hashes this module makes.

  * Passwords: salted scrypt, stored as 'scrypt$N$r$p$salt-hex$hash-hex'.
  * Invite codes and device tokens: random, and stored only as SHA-256
    hashes. They are high-entropy, so a fast hash is enough.
'''

import base64
import hashlib
import hmac
import re
import secrets

from evals_service.evals_common import EvalsCommon


class Credentials:
    '''
    Stateless helpers; never instantiated.
    '''

    SCRYPT_N = 2 ** 14       # about 16 MiB and 50 ms
    SCRYPT_R = 8
    SCRYPT_P = 1
    SALT_BYTES = 16
    HASH_BYTES = 32

    # Crockford base32 without look-alikes, for codes people copy by hand:
    CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
    EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

    #------------------------------------
    # normalize_email
    #-------------------

    @classmethod
    def normalize_email(cls, email):
        '''
        :param email: address as typed
        :type email: str
        :return: lower-cased, stripped address
        :rtype: str
        :raises ValueError: if it does not look like an address
        '''
        email = (email or '').strip().lower()
        if len(email) > 254 or not cls.EMAIL_RE.match(email):
            raise ValueError("not a valid e-mail address")
        return email

    #------------------------------------
    # check_password_policy
    #-------------------

    @classmethod
    def check_password_policy(cls, password):
        '''
        :param password: proposed new password
        :type password: str
        :raises ValueError: if it is too short or too long
        '''
        if not isinstance(password, str) or len(password) < EvalsCommon.PASSWORD_MIN_LEN:
            raise ValueError(f"The password needs at least {EvalsCommon.PASSWORD_MIN_LEN} characters")
        if len(password) > EvalsCommon.PASSWORD_MAX_LEN:
            raise ValueError("The password is too long")

    #------------------------------------
    # hash_password
    #-------------------

    @classmethod
    def hash_password(cls, password):
        '''
        :param password: clear text
        :type password: str
        :return: self-describing hash string
        :rtype: str
        '''
        salt = secrets.token_bytes(cls.SALT_BYTES)
        digest = cls._scrypt(password, salt, cls.SCRYPT_N, cls.SCRYPT_R, cls.SCRYPT_P)
        return '$'.join(['scrypt', str(cls.SCRYPT_N), str(cls.SCRYPT_R), str(cls.SCRYPT_P),
                         salt.hex(), digest.hex()])

    #------------------------------------
    # verify_password
    #-------------------

    @classmethod
    def verify_password(cls, password, stored):
        '''
        :param password: clear text to test
        :type password: str
        :param stored: value made by hash_password; None if the account has no password
        :type stored: optional[str]
        :return: True if they match. Takes the same time for a missing hash.
        :rtype: bool
        '''
        try:
            scheme, n, r, p, salt_hex, hash_hex = (stored or '').split('$')
            if scheme != 'scrypt':
                raise ValueError(scheme)
            salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
            n, r, p = int(n), int(r), int(p)
        except ValueError:
            # Spend the time anyway, so that a missing account cannot be told apart:
            cls._scrypt(password, b'\0' * cls.SALT_BYTES, cls.SCRYPT_N, cls.SCRYPT_R, cls.SCRYPT_P)
            return False
        return hmac.compare_digest(cls._scrypt(password, salt, n, r, p), expected)

    #------------------------------------
    # new_invite_code
    #-------------------

    @classmethod
    def new_invite_code(cls):
        '''
        :return: a code such as 'K7M2-9QXP-4TBD', easy to read aloud
        :rtype: str
        '''
        raw = ''.join(secrets.choice(cls.CODE_ALPHABET) for _ in range(12))
        return '-'.join(raw[i:i + 4] for i in range(0, 12, 4))

    #------------------------------------
    # normalize_code
    #-------------------

    @classmethod
    def normalize_code(cls, code):
        '''
        Typed codes tolerate case, spaces, dashes and the look-alikes O and I, L.

        :param code: as typed
        :type code: str
        :return: canonical form without dashes
        :rtype: str
        '''
        text = re.sub(r'[\s-]', '', (code or '').upper())
        return text.replace('O', '0').replace('I', '1').replace('L', '1')

    #------------------------------------
    # hash_code
    #-------------------

    @classmethod
    def hash_code(cls, code):
        '''
        :param code: invite code as typed
        :type code: str
        :return: hex SHA-256 of the canonical form
        :rtype: str
        '''
        return hashlib.sha256(cls.normalize_code(code).encode('utf-8')).hexdigest()

    #------------------------------------
    # new_device_token
    #-------------------

    @classmethod
    def new_device_token(cls):
        '''
        :return: (token for the browser, hash for the database)
        :rtype: tuple[str, str]
        '''
        token = base64.urlsafe_b64encode(secrets.token_bytes(EvalsCommon.DEVICE_TOKEN_BYTES)).decode('ascii').rstrip('=')
        return token, cls.hash_token(token)

    #------------------------------------
    # hash_token
    #-------------------

    @classmethod
    def hash_token(cls, token):
        '''
        :param token: device token as the browser holds it
        :type token: str
        :return: hex SHA-256
        :rtype: str
        '''
        return hashlib.sha256((token or '').encode('utf-8')).hexdigest()

    # ------------------------- Private -------------------------

    @classmethod
    def _scrypt(cls, password, salt, n, r, p):
        return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                              dklen=cls.HASH_BYTES, maxmem=128 * n * r * 2 + 2 ** 20)
