# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/evals_common.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
evals_common.py

Class-level constants shared by the evaluation service modules.
Values that must agree with the producer side (the therapist repo's
student_eval/evals_common.py), such as the bundle format version and
the role colors, are checked against each bundle's manifest at load.

Nothing secret lives here. The service reads its secrets, if any, from
files under its own directory.
'''

from pathlib import Path


class EvalsCommon:
    '''
    Constants only; never instantiated.
    '''

    # ------------------------- Bundles (from the trainer's server) -------------------

    # Must equal BUNDLE_FORMAT_VERSION on the producer side:
    BUNDLE_FORMAT_VERSION = 1

    # Where the push agent puts bundles (owned by the upload account, readable by the service):
    INBOX_DIR = Path('/srv/thdata/inbox/evals')

    # A bundle is two files in the inbox, pushed in this order, so that the manifest is
    # the commit marker. <id> is the manifest's bundle_id.
    SESSIONS_FILE_FORMAT = 'sessions-{bundle_id}.jsonl'
    MANIFEST_FILE_FORMAT = 'manifest-{bundle_id}.json'
    MANIFEST_GLOB = 'manifest-*.json'

    # Hex colors for the four dialog roles; must equal those of the producer side:
    ROLE_COLORS = {
        'client'   : '#B83404',
        'therapist': '#228B22',
        'advisee'  : '#5E2CA5',
        'advisor'  : '#0072B2',
        }

    # Releases kept on disk, newest first:
    RELEASES_KEPT = 5

    # ------------------------- Service layout -------------------

    SERVICE_ROOT      = Path('/srv/evals')
    RELEASES_DIR_NAME = 'releases'
    CURRENT_LINK_NAME = 'current'
    DATA_DIR_NAME     = 'data'            # SQLite database; the only copy of the ratings
    CONFIG_DIR_NAME   = 'config'          # roster.json
    DB_NAME           = 'evals.sqlite'
    ROSTER_NAME       = 'roster.json'

    # ------------------------- Server -------------------

    DEFAULT_BIND = '127.0.0.1'
    DEFAULT_PORT = 8082

    # ------------------------- People and accounts -------------------

    ROLE_ADMIN     = 'admin'
    ROLE_EVALUATOR = 'evaluator'   # reviews sessions; Evaluator_<n>
    ROLE_STUDENT   = 'student'     # self-assessments only
    ALL_ROLES      = (ROLE_ADMIN, ROLE_EVALUATOR, ROLE_STUDENT)

    # Display name of evaluator number n is f'{EVALUATOR_LABEL_PREFIX}{n}':
    EVALUATOR_LABEL_PREFIX = 'Evaluator_'

    PASSWORD_MIN_LEN = 10
    PASSWORD_MAX_LEN = 200

    # An invite code lets its holder choose a password once:
    INVITE_CODE_BYTES = 8
    INVITE_VALID_DAYS = 30

    # A browser that logged in once keeps a random token in localStorage and needs no password
    # until the token expires unused:
    DEVICE_TOKEN_BYTES = 32
    DEVICE_TOKEN_VALID_DAYS = 180

    # Password guesses per (client address, e-mail) before a pause:
    LOGIN_MAX_FAILURES = 5
    LOGIN_LOCK_SECS = 300

    # ------------------------- Rating forms -------------------

    FORM_INSTRUCTOR_ASSESSMENT = 'instructor_assessment'   # instructor rates own student, unblinded
    FORM_SELF_ASSESSMENT       = 'self_assessment'         # student rates self
    FORM_SESSION_REVIEW        = 'session_review'          # blinded; student skills and trainer
    ALL_FORMS = (FORM_INSTRUCTOR_ASSESSMENT, FORM_SELF_ASSESSMENT, FORM_SESSION_REVIEW)

    STATE_DRAFT     = 'draft'
    STATE_SUBMITTED = 'submitted'

    NA = 'n/a'            # answer value for "not applicable"
    MAX_COMMENT_LEN = 4000

    # ------------------------- Admin view -------------------

    # An account that has not been active for this long is flagged for a reminder:
    REMINDER_AFTER_DAYS = 7

    # Do not rescan the inbox for new bundles more often than this:
    BUNDLE_RESCAN_SECS = 15
