# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-03 23:50:00
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/evals_common.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-03 23:50:00
# **********************************************************

'''
evals_common.py

Class-level constants shared by the evaluation service modules.
Values that must agree with the producer side (the therapist repo's
student_eval/evals_common.py), such as the bundle format version and
the role colors, are checked against each bundle's manifest at load.
'''

from pathlib import Path


class EvalsCommon:
    '''
    Constants only; never instantiated.
    '''

    # Must equal BUNDLE_FORMAT_VERSION on the producer side:
    BUNDLE_FORMAT_VERSION = 1

    # Layout under the service root (default /srv/evals):
    SERVICE_ROOT      = Path('/srv/evals')
    INBOX_DIR_NAME    = 'inbox'
    RELEASES_DIR_NAME = 'releases'
    CURRENT_LINK_NAME = 'current'
    DATA_DIR_NAME     = 'data'            # SQLite ratings; outside releases
    RATINGS_DB_NAME   = 'ratings.sqlite'

    # Bundle file names:
    MANIFEST_NAME = 'manifest.json'
    SESSIONS_NAME = 'sessions.jsonl'

    # Secrets are read from files, never from code:
    SECRETS_DIR = Path.home() / '.ssh'
    TOKEN_SALT_FILE = 'eval_rater_token_secret.txt'

    RATING_MIN = 1
    RATING_MAX = 7
