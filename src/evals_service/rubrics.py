# **********************************************************
# @Author: Andreas Paepcke
# @Date:   2026-10-11 16:46:16
# @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/rubrics.py
# @Last Modified by:   Andreas Paepcke
# @Last Modified time: 2026-10-11 16:46:16
# **********************************************************

'''
rubrics.py

The rubrics and the form definitions, read from data/rubrics.json, and the
checking of submitted answers against them. Edit the wording in the JSON
file, not here. A change to the file changes the rubric version, which is
stored with every rating.

An answer set is a dict:

    {"student": {"<item id>": 7, ...},
     "trainer": {"<item id>": 4, "<other item id>": "n/a", ...},
     "comments": {"student": "free text", "trainer": "free text"}}

Only the sections that the form has appear in it.
'''

import copy
import hashlib
import json
from pathlib import Path

from evals_service.evals_common import EvalsCommon


class RubricLibrary:
    '''
    Immutable view of rubrics.json.
    '''

    DEFAULT_PATH = Path(__file__).parent / 'data' / 'rubrics.json'

    #------------------------------------
    # Constructor
    #-------------------

    def __init__(self, path=None):
        '''
        :param path: rubrics file; defaults to the one shipped with the package
        :type path: optional[union[str, Path]]
        :raises ValueError: if the file is internally inconsistent
        '''
        raw = Path(path or self.DEFAULT_PATH).read_bytes()
        self._doc = json.loads(raw.decode('utf-8'))
        self.version = f"{self._doc['version']}-{hashlib.sha256(raw).hexdigest()[:8]}"
        self._check()

    #------------------------------------
    # form_kinds
    #-------------------

    def form_kinds(self):
        '''
        :return: names of all forms
        :rtype: list[str]
        '''
        return list(self._doc['forms'])

    #------------------------------------
    # is_blinded
    #-------------------

    def is_blinded(self, kind):
        '''
        :param kind: form kind
        :type kind: str
        :return: True if the evaluator must not learn who the student is
        :rtype: bool
        '''
        return bool(self._form(kind)['blinded'])

    #------------------------------------
    # sections_of
    #-------------------

    def sections_of(self, kind):
        '''
        :param kind: form kind
        :type kind: str
        :return: the rubric names in the form, in display order
        :rtype: list[str]
        '''
        return list(self._form(kind)['sections'])

    #------------------------------------
    # form_for_display
    #-------------------

    def form_for_display(self, kind, defense=None):
        '''
        The form as the browser needs it: titles, items with scale and band
        texts, instructions. Placeholders in prompts are filled in.

        :param kind: form kind
        :type kind: str
        :param defense: the intended client defense, shown inside one trainer
            item; None shows 'not recorded'
        :type defense: optional[str]
        :return: a JSON-serializable dict
        :rtype: dict
        '''
        form = self._form(kind)
        sections = []
        for name in form['sections']:
            rubric = copy.deepcopy(self._doc['rubrics'][name])
            for item in rubric['items']:
                if 'prompt' in item:
                    item['prompt'] = item['prompt'].replace('{defense}', defense or 'not recorded')
            sections.append({'name': name, **rubric})
        return {
            'kind': kind,
            'title': form['title'],
            'blinded': bool(form['blinded']),
            'version': self.version,
            'instructions': self._doc['instructions'] if 'student' in form['sections'] else None,
            'sections': sections,
            'na': EvalsCommon.NA,
            'max_comment_len': EvalsCommon.MAX_COMMENT_LEN,
            }

    #------------------------------------
    # clean_answers
    #-------------------

    def clean_answers(self, kind, answers, complete):
        '''
        Check answers and return a normalized copy.

        :param kind: form kind
        :type kind: str
        :param answers: what the browser sent
        :type answers: dict
        :param complete: True when the rating is being submitted, so that
            every item must be answered; False for a draft
        :type complete: bool
        :return: answers with int values (or the NA marker) and trimmed comments
        :rtype: dict
        :raises ValueError: with a message naming what is wrong
        '''
        if not isinstance(answers, dict):
            raise ValueError("answers must be an object")
        sections = self.sections_of(kind)
        unknown = set(answers) - set(sections) - {'comments'}
        if unknown:
            raise ValueError(f"unknown answer part(s): {sorted(unknown)}")
        cleaned = {}
        missing = []
        for name in sections:
            rubric = self._doc['rubrics'][name]
            scale = rubric['scale']
            given = answers.get(name) or {}
            if not isinstance(given, dict):
                raise ValueError(f"answers for '{name}' must be an object")
            known = {item['id']: item for item in rubric['items']}
            if set(given) - set(known):
                raise ValueError(f"unknown item(s) in '{name}': {sorted(set(given) - set(known))}")
            section = {}
            for item_id, item in known.items():
                if item_id not in given or given[item_id] is None:
                    missing.append(f"{name}/{item_id}")
                    continue
                section[item_id] = self._clean_value(given[item_id], scale, item, f"{name}/{item_id}")
            cleaned[name] = section
        comments = answers.get('comments') or {}
        if not isinstance(comments, dict) or set(comments) - set(sections):
            raise ValueError("comments must be an object with one entry per section")
        cleaned['comments'] = {}
        for name, text in comments.items():
            if not isinstance(text, str):
                raise ValueError(f"comment for '{name}' must be text")
            text = text.strip()
            if len(text) > EvalsCommon.MAX_COMMENT_LEN:
                raise ValueError(f"comment for '{name}' is longer than {EvalsCommon.MAX_COMMENT_LEN} characters")
            if text:
                cleaned['comments'][name] = text
        if complete and missing:
            raise ValueError(f"unanswered: {', '.join(missing)}")
        return cleaned

    #------------------------------------
    # item_ids
    #-------------------

    def item_ids(self, kind):
        '''
        :param kind: form kind
        :type kind: str
        :return: (section name, item id) for every item of the form, in order
        :rtype: list[tuple[str, str]]
        '''
        return [(name, item['id'])
                for name in self.sections_of(kind)
                for item in self._doc['rubrics'][name]['items']]

    # ------------------------- Private -------------------------

    def _form(self, kind):
        try:
            return self._doc['forms'][kind]
        except KeyError:
            raise ValueError(f"unknown form '{kind}'") from None

    @staticmethod
    def _clean_value(value, scale, item, where):
        if value == EvalsCommon.NA:
            if not item.get('na_allowed', scale.get('na_allowed', False)):
                raise ValueError(f"'{EvalsCommon.NA}' is not allowed for {where}")
            return EvalsCommon.NA
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} must be a whole number")
        if not scale['min'] <= value <= scale['max']:
            raise ValueError(f"{where} must be between {scale['min']} and {scale['max']}")
        return value

    def _check(self):
        for name in ('version', 'instructions', 'rubrics', 'forms'):
            if name not in self._doc:
                raise ValueError(f"rubrics file lacks '{name}'")
        for rubric_name, rubric in self._doc['rubrics'].items():
            seen = set()
            low, high = rubric['scale']['min'], rubric['scale']['max']
            for item in rubric['items']:
                if item['id'] in seen:
                    raise ValueError(f"duplicate item id '{item['id']}' in '{rubric_name}'")
                seen.add(item['id'])
                bands = item.get('bands')
                if bands:
                    covered = [v for band in bands for v in range(band['min'], band['max'] + 1)]
                    if sorted(covered) != list(range(low, high + 1)):
                        raise ValueError(f"bands of '{item['id']}' do not cover {low}..{high} exactly once")
        for kind, form in self._doc['forms'].items():
            for section in form['sections']:
                if section not in self._doc['rubrics']:
                    raise ValueError(f"form '{kind}' uses unknown rubric '{section}'")
        missing = set(EvalsCommon.ALL_FORMS) - set(self._doc['forms'])
        if missing:
            raise ValueError(f"rubrics file lacks form(s) {sorted(missing)}")
