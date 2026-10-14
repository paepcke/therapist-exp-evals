# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/workflow.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
workflow.py

What each person is asked to rate, in what order, and who may rate what.
The administrator's progress report is built here too.

Subjects (what a rating is about):

  session_review          the session's id, from the active bundle
  instructor_assessment   '<milestone id>|<student id>', by an instructor of the student's cohort
  self_assessment         '<milestone id>|<student id>', by the student, about the student

The order of session reviews is random but fixed per person: the sessions are
sorted by a hash of (person, session), so the order cannot be read off the
sessions' dates and stays the same from visit to visit. A draft is always offered
first, so that nobody is left with a half-finished rating.
'''

import hashlib
from datetime import date, datetime, timezone

from evals_service.evals_common import EvalsCommon


class AccessError(Exception):
    '''
    The person may not rate this subject (or it does not exist).
    '''


class Workflow:
    '''
    Combines store, roster, bundle and rubrics.
    '''

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, store, roster, bundles, rubrics, clock=None):
        '''
        :param store: the database
        :type store: EvalStore
        :param roster: who teaches whom
        :type roster: Roster
        :param bundles: sessions to review
        :type bundles: BundleStore
        :param rubrics: the forms
        :type rubrics: RubricLibrary
        :param clock: returns an aware datetime; for tests
        :type clock: optional[callable]
        '''
        self._store = store
        self._roster = roster
        self._bundles = bundles
        self._rubrics = rubrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def set_roster(self, roster):
        '''
        :param roster: a newly loaded roster
        :type roster: Roster
        '''
        self._roster = roster

    #------------------------------------
    # overview
    #-------------------

    def overview(self, person):
        '''
        Everything the start page needs.

        :param person: the logged-in person (row of the people table)
        :type person: dict
        :return: assessments due and review counts
        :rtype: dict
        '''
        roles = person['roles'].split(',')
        out = {'assessments': [], 'reviews': None}
        out['assessments'] = self._assessment_tasks(person)
        if EvalsCommon.ROLE_EVALUATOR in roles:
            out['reviews'] = self._review_counts(person)
        return out

    #------------------------------------
    # next_review
    #-------------------

    def next_review(self, person):
        '''
        :param person: an evaluator
        :type person: dict
        :return: the session id to show next: the oldest draft, else the first unrated session in
            this person's order; None when everything is rated
        :rtype: optional[str]
        '''
        self._require_role(person, EvalsCommon.ROLE_EVALUATOR)
        mine = {r['subject_key']: r for r in self._store.ratings_of(person['id'], EvalsCommon.FORM_SESSION_REVIEW)}
        drafts = sorted((r for r in mine.values() if r['state'] == EvalsCommon.STATE_DRAFT
                         and self._bundles.session(r['subject_key']) is not None),
                        key=lambda r: r['updated_utc'])
        if drafts:
            return drafts[0]['subject_key']
        for sid in self._order(person, self._bundles.session_ids()):
            if sid not in mine:
                return sid
        return None

    #------------------------------------
    # resolve
    #-------------------

    def resolve(self, person, form_kind, subject_key):
        '''
        Check that the person may rate the subject, and describe it.

        :param person: who asks
        :type person: dict
        :param form_kind: one of EvalsCommon.ALL_FORMS
        :type form_kind: str
        :param subject_key: what is rated
        :type subject_key: str
        :return: {'defense': ..., 'subject': {...for the page...}}
        :rtype: dict
        :raises AccessError: if not allowed or unknown
        '''
        if form_kind == EvalsCommon.FORM_SESSION_REVIEW:
            self._require_role(person, EvalsCommon.ROLE_EVALUATOR)
            session = self._bundles.session(subject_key)
            if session is None:
                raise AccessError("That session is not available.")
            return {'defense': session['defense'],
                    'subject': {'kind': 'session', 'id': session['session_id'], 'html': session['html'],
                                'critiques': session['critiques']}}
        if form_kind in (EvalsCommon.FORM_INSTRUCTOR_ASSESSMENT, EvalsCommon.FORM_SELF_ASSESSMENT):
            milestone_id, _, student_id = subject_key.partition('|')
            milestone = self._roster.milestone(milestone_id)
            student = self._roster.student_by_id(student_id)
            if milestone is None or student is None or milestone.opens > self._today():
                raise AccessError("That assessment is not available.")
            if form_kind == EvalsCommon.FORM_SELF_ASSESSMENT:
                allowed = student.email == person['email']
            else:
                allowed = student.email in [s.email for s in self._roster.students_taught_by(person['email'])]
            if not allowed:
                raise AccessError("That assessment is not available.")
            return {'defense': None,
                    'subject': {'kind': 'student', 'id': student.id, 'name': student.name,
                                'milestone': milestone.label}}
        raise AccessError("Unknown form.")

    #------------------------------------
    # progress
    #-------------------

    def progress(self):
        '''
        The administrator's view: one row per person, and totals.

        :return: JSON-serializable dict
        :rtype: dict
        '''
        today = self._today()
        now = self._clock()
        people = self._store.all_people()
        rows = []
        for person in people:
            roles = person['roles'].split(',')
            row = {'label': person['label'], 'email': person['email'], 'name': person['name'], 'roles': roles,
                   'activated': person['activated_utc'] is not None, 'disabled': bool(person['disabled']),
                   'last_seen_utc': person['last_seen_utc'], 'days_idle': self._days_idle(person, now),
                   'invite_pending': person['invite_hash'] is not None}
            row['reviews'] = self._review_counts(person) if EvalsCommon.ROLE_EVALUATOR in roles else None
            row['assessments'] = self._assessment_summary(person, today)
            row['outstanding'] = self._outstanding(row)
            row['needs_reminder'] = self._needs_reminder(person, row)
            rows.append(row)
        return {'generated_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'bundle': self._bundles.info(),
                'milestones': [{'id': m.id, 'label': m.label, 'opens': m.opens.isoformat()}
                               for m in self._roster.milestones],
                'rows': rows,
                'reminder_after_days': EvalsCommon.REMINDER_AFTER_DAYS}

    # ------------------------- Private -------------------------

    def _today(self):
        return self._clock().date()

    @staticmethod
    def _require_role(person, role):
        if role not in person['roles'].split(','):
            raise AccessError("Your account may not do that.")

    @staticmethod
    def _order(person, session_ids):
        return sorted(session_ids, key=lambda sid: hashlib.sha256(f"{person['id']}:{sid}".encode()).hexdigest())

    def _review_counts(self, person):
        ids = set(self._bundles.session_ids())
        mine = self._store.ratings_of(person['id'], EvalsCommon.FORM_SESSION_REVIEW)
        submitted = {r['subject_key'] for r in mine if r['state'] == EvalsCommon.STATE_SUBMITTED}
        drafts = {r['subject_key'] for r in mine if r['state'] == EvalsCommon.STATE_DRAFT}
        seen = self._store.seen_of(person['id'], EvalsCommon.FORM_SESSION_REVIEW)
        return {'available': len(ids),
                'submitted': len(submitted & ids),
                'draft': len(drafts & ids),
                'opened_not_rated': len((seen & ids) - submitted - drafts),
                'remaining': len(ids - submitted)}

    def _assessment_tasks(self, person):
        roles = person['roles'].split(',')
        today = self._today()
        tasks = []
        mine = {(r['form_kind'], r['subject_key']): r['state'] for r in self._store.ratings_of(person['id'])
                if r['form_kind'] != EvalsCommon.FORM_SESSION_REVIEW}
        for milestone in self._roster.open_milestones(today):
            student = self._roster.student_by_email(person['email'])
            if EvalsCommon.ROLE_STUDENT in roles and student is not None:
                key = f'{milestone.id}|{student.id}'
                tasks.append({'form': EvalsCommon.FORM_SELF_ASSESSMENT, 'subject': key,
                              'title': f'Self-assessment: {milestone.label}',
                              'state': mine.get((EvalsCommon.FORM_SELF_ASSESSMENT, key), 'new')})
            for s in self._roster.students_taught_by(person['email']):
                key = f'{milestone.id}|{s.id}'
                tasks.append({'form': EvalsCommon.FORM_INSTRUCTOR_ASSESSMENT, 'subject': key,
                              'title': f'{s.name}: {milestone.label}',
                              'state': mine.get((EvalsCommon.FORM_INSTRUCTOR_ASSESSMENT, key), 'new')})
        return tasks

    def _assessment_summary(self, person, today):
        tasks = self._assessment_tasks(person)
        out = {'expected': len(tasks),
               'submitted': sum(1 for t in tasks if t['state'] == EvalsCommon.STATE_SUBMITTED),
               'draft': sum(1 for t in tasks if t['state'] == EvalsCommon.STATE_DRAFT)}
        return out

    @staticmethod
    def _outstanding(row):
        total = 0
        if row['reviews'] is not None:
            total += row['reviews']['remaining']
        total += row['assessments']['expected'] - row['assessments']['submitted']
        return total

    @staticmethod
    def _days_idle(person, now):
        stamp = person['last_seen_utc'] or person['activated_utc']
        if stamp is None:
            return None
        then = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return (now - then).days

    @staticmethod
    def _needs_reminder(person, row):
        if person['disabled']:
            return False
        if not row['activated']:
            return True
        if row['outstanding'] <= 0:
            return False
        return (row['days_idle'] or 0) >= EvalsCommon.REMINDER_AFTER_DAYS
