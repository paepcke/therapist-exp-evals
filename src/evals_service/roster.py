# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/roster.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
roster.py

Who teaches whom, and when assessments open. Read from
/srv/evals/config/roster.json, which the administrator writes by hand on
this machine. It is never pushed from the trainer's server, so the trainer's
pseudonym secret and the students' trainer logs stay out of reach of anyone
who can read this file.

    {
      "milestones": [
        {"id": "m1", "label": "After the November weekend", "opens": "2026-11-15"}
      ],
      "cohorts": [
        {"id": "A", "name": "Cohort A",
         "instructors": ["jon@example.org"],
         "students": [
            {"email": "s1@example.org", "name": "Student One", "code": "1a2b3c4d"}
         ]}
      ]
    }

'code' is optional: the participant code of the student in the trainer's logs
(computed on the trainer's server). The service never uses it; it is kept so
that the analysis can join assessments to usage.
'''

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from evals_service.credentials import Credentials


@dataclass(frozen=True)
class Milestone:
    id: str
    label: str
    opens: date


@dataclass(frozen=True)
class StudentEntry:
    id: str                 # stable short id, used in rating keys
    email: str
    name: str
    code: str | None
    cohort_id: str


@dataclass
class Cohort:
    id: str
    name: str
    instructors: list = field(default_factory=list)   # normalized e-mail addresses
    students: list = field(default_factory=list)      # list[StudentEntry]


class Roster:
    '''
    Immutable view of roster.json.
    '''

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, doc=None):
        '''
        :param doc: parsed roster; None means an empty roster
        :type doc: optional[dict]
        :raises ValueError: if the roster is inconsistent
        '''
        doc = doc or {}
        self.milestones = []
        self.cohorts = []
        seen_ids = set()
        for m in doc.get('milestones', []):
            if m['id'] in seen_ids:
                raise ValueError(f"duplicate milestone id '{m['id']}'")
            seen_ids.add(m['id'])
            if '|' in m['id']:
                raise ValueError("milestone ids must not contain '|'")
            self.milestones.append(Milestone(m['id'], m.get('label', m['id']), date.fromisoformat(m['opens'])))
        self.milestones.sort(key=lambda m: m.opens)
        emails = set()
        for c in doc.get('cohorts', []):
            cohort = Cohort(c['id'], c.get('name', c['id']),
                            [Credentials.normalize_email(e) for e in c.get('instructors', [])])
            for s in c.get('students', []):
                email = Credentials.normalize_email(s['email'])
                if email in emails:
                    raise ValueError(f"student {email} is listed twice")
                emails.add(email)
                sid = s.get('id') or 'S' + hashlib.sha256(email.encode('utf-8')).hexdigest()[:6]
                cohort.students.append(StudentEntry(sid, email, s.get('name') or email.split('@')[0],
                                                    s.get('code'), cohort.id))
            self.cohorts.append(cohort)
        ids = [s.id for c in self.cohorts for s in c.students]
        if len(ids) != len(set(ids)):
            raise ValueError("student ids are not unique")

    @classmethod
    def load(cls, path):
        '''
        :param path: roster.json; a missing file gives an empty roster
        :type path: union[str, Path]
        :return: the roster
        :rtype: Roster
        '''
        path = Path(path)
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text(encoding='utf-8')))

    #------------------------------------
    # open_milestones
    #-------------------

    def open_milestones(self, today):
        '''
        :param today: the date to test
        :type today: datetime.date
        :return: milestones that have opened, oldest first
        :rtype: list[Milestone]
        '''
        return [m for m in self.milestones if m.opens <= today]

    def milestone(self, milestone_id):
        '''
        :param milestone_id: id
        :type milestone_id: str
        :return: the milestone, or None
        :rtype: optional[Milestone]
        '''
        return next((m for m in self.milestones if m.id == milestone_id), None)

    #------------------------------------
    # students_taught_by
    #-------------------

    def students_taught_by(self, email):
        '''
        :param email: instructor's normalized address
        :type email: str
        :return: all students of all cohorts the person instructs
        :rtype: list[StudentEntry]
        '''
        return [s for c in self.cohorts if email in c.instructors for s in c.students]

    def student_by_email(self, email):
        '''
        :param email: normalized address
        :type email: str
        :return: the entry, or None
        :rtype: optional[StudentEntry]
        '''
        for c in self.cohorts:
            for s in c.students:
                if s.email == email:
                    return s
        return None

    def student_by_id(self, student_id):
        '''
        :param student_id: id
        :type student_id: str
        :return: the entry, or None
        :rtype: optional[StudentEntry]
        '''
        for c in self.cohorts:
            for s in c.students:
                if s.id == student_id:
                    return s
        return None

    def all_emails(self):
        '''
        :return: (email, name, kind) for each instructor ('instructor') and student ('student')
        :rtype: list[tuple[str, str, str]]
        '''
        out, seen = [], set()
        for c in self.cohorts:
            for e in c.instructors:
                if e not in seen:
                    seen.add(e)
                    out.append((e, e.split('@')[0], 'instructor'))
            for s in c.students:
                out.append((s.email, s.name, 'student'))
        return out
