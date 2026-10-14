/**
 * **********************************************************
 * @Author: Andreas Paepcke
 * @Date:   2026-10-11 16:46:16
 * @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/static/forms.mjs
 * @Last Modified by:   Andreas Paepcke
 * @Last Modified time: 2026-10-11 16:46:16
 * **********************************************************
 */

'use strict';

/*
 * One rating form: its sections, the subject being rated (a session, or a
 * student), autosave of drafts and the final, irreversible submit.
 */

import { Dom } from './dom.mjs';
import { ApiError } from './api.mjs';

export class FormView {
  static AUTOSAVE_DELAY_MS = 1200;

  /**
   * @param {Api} api
   * @param {Object} data    the /api/item reply
   * @param {Function} onDone  called with 'next' or 'home' when the person leaves after submitting
   */
  constructor(api, data, onDone) {
    this.api = api;
    this.form = data.form;
    this.subject = data.subject;
    this.subjectKey = data.key;
    this.rating = data.rating;
    this.onDone = onDone;
    this.answers = this._initialAnswers(data.rating);
    this.submitted = !!(data.rating && data.rating.state === 'submitted');
    this.dirty = false;
    this.saving = false;
    this._timer = null;
    this._itemBoxes = new Map();    // 'section/item' -> fieldset
    this.root = Dom.el('div', { class: 'form-view' });
    this._build();
  }

  dispose() {
    clearTimeout(this._timer);
    document.removeEventListener('visibilitychange', this._onHide);
    window.removeEventListener('pagehide', this._onHide);
  }

  // ---------------------------------------------------------------- building

  _initialAnswers(rating) {
    const answers = { comments: {} };
    for (const section of this.form.sections) answers[section.name] = {};
    if (rating) {
      for (const [name, part] of Object.entries(rating.answers)) answers[name] = { ...part };
    }
    return answers;
  }

  _build() {
    const form = this.form;
    this.root.append(Dom.el('h1', { text: form.title }));
    this.root.append(this._subjectBlock());
    if (this.submitted) {
      this.root.append(Dom.el('p', { class: 'notice ok', text: 'This rating was submitted and cannot be changed.' }));
    }
    if (form.instructions) this.root.append(this._instructions(form.instructions));
    form.sections.forEach((section, index) => this.root.append(this._section(section, index)));
    this.root.append(this._footer());
    this._updateProgress();
    this._onHide = (e) => { if (e.type === 'pagehide' || document.visibilityState === 'hidden') this._flush(true); };
    document.addEventListener('visibilitychange', this._onHide);
    window.addEventListener('pagehide', this._onHide);
  }

  _subjectBlock() {
    const subject = this.subject;
    if (subject.kind === 'session') {
      const holder = Dom.el('div', { class: 'session-holder' });
      Dom.sessionHtml(holder, subject.html);
      const box = Dom.el('div', { class: 'subject' }, holder);
      if (subject.critiques && subject.critiques.length) {
        const details = Dom.el('details', { class: 'critiques', open: true },
          Dom.el('summary', { text: `Session Critique${subject.critiques.length > 1 ? 's' : ''} written by the trainer` }));
        subject.critiques.forEach((text, i) => {
          details.append(Dom.el('h3', { text: subject.critiques.length > 1 ? `Critique ${i + 1}` : 'Critique' }));
          text.split(/\n{2,}/).forEach(par => details.append(Dom.el('p', { class: 'critique-text', text: par })));
        });
        box.append(details);
      } else {
        box.append(Dom.el('p', { class: 'muted', text: 'The student did not request a Session Critique in this case.' }));
      }
      return box;
    }
    return Dom.el('div', { class: 'subject card' },
      Dom.el('p', { class: 'subject-name', text: subject.name }),
      Dom.el('p', { class: 'muted', text: subject.milestone }));
  }

  _instructions(instructions) {
    const list = Dom.el('dl');
    instructions.items.forEach(item => {
      list.append(Dom.el('dt', { text: item.label }), Dom.el('dd', { text: item.text }));
    });
    return Dom.el('details', { class: 'card instructions' }, Dom.el('summary', { text: instructions.title }), list);
  }

  _section(section, index) {
    const box = Dom.el('section', { class: 'rubric' },
      Dom.el('h2', { text: this.form.sections.length > 1 ? `Part ${index + 1}: ${section.title}` : section.title }));
    if (section.name === 'trainer') {
      box.append(Dom.el('p', { class: 'muted', text: 'These questions are about the trainer program, not the student.' }));
    }
    section.items.forEach((item, i) => box.append(this._item(section, item, i + 1)));
    const area = Dom.el('textarea', {
      class: 'comment', rows: 3, maxlength: this.form.max_comment_len, 'aria-label': `Optional comments, ${section.title}`,
      placeholder: 'Comments (optional)',
    });
    area.value = this.answers.comments[section.name] || '';
    area.disabled = this.submitted;
    area.addEventListener('input', () => {
      this.answers.comments[section.name] = area.value;
      this._changed();
    });
    box.append(area);
    return box;
  }

  _item(section, item, number) {
    const key = `${section.name}/${item.id}`;
    const box = Dom.el('fieldset', { class: 'item', id: `item-${section.name}-${item.id}` });
    const title = item.name || item.prompt;
    box.append(Dom.el('legend', {}, `${number}. `, item.prompt ? Dom.emphasized(item.prompt) : title));
    if (item.definition) box.append(Dom.el('p', { class: 'definition', text: item.definition }));

    const choices = Dom.el('div', { class: 'choices', role: 'radiogroup' });
    const low = section.scale.min, high = section.scale.max;
    for (let value = low; value <= high; value++) choices.append(this._choice(section, item, value, String(value)));
    if (item.na_allowed) choices.append(this._choice(section, item, this.form.na, 'n/a', 'na'));
    box.append(choices);
    if (item.anchors) {
      box.append(Dom.el('div', { class: 'anchors' }, Dom.el('span', { text: `${low} = ${item.anchors[0]}` }),
                        Dom.el('span', { text: `${high} = ${item.anchors[1]}` })));
    }
    if (item.bands) box.append(this._bands(item, section.name));
    this._itemBoxes.set(key, box);
    this._markBand(section.name, item);
    return box;
  }

  _choice(section, item, value, text, extra = '') {
    const id = `r-${section.name}-${item.id}-${String(value).replace('/', '')}`;
    const input = Dom.el('input', { type: 'radio', name: `${section.name}-${item.id}`, id, value: String(value) });
    input.checked = this.answers[section.name][item.id] === value;
    input.disabled = this.submitted;
    input.addEventListener('change', () => {
      this.answers[section.name][item.id] = value;
      this._markBand(section.name, item);
      this._itemBoxes.get(`${section.name}/${item.id}`).classList.remove('missing');
      this._changed();
    });
    return Dom.el('label', { class: `choice ${extra}`, for: id }, input, Dom.el('span', { text }));
  }

  _bands(item, sectionName) {
    const list = Dom.el('ul', { class: 'bands', 'data-for': `${sectionName}/${item.id}` });
    item.bands.forEach(band => {
      list.append(Dom.el('li', { 'data-min': band.min, 'data-max': band.max },
        Dom.el('strong', { text: `${band.min}–${band.max} ${band.label}` }),
        Dom.el('span', { text: ` ${band.text}` })));
    });
    return list;
  }

  _markBand(sectionName, item) {
    if (!item.bands) return;
    const value = this.answers[sectionName][item.id];
    const list = this._itemBoxes.get(`${sectionName}/${item.id}`)?.querySelector('.bands');
    if (!list) return;
    for (const li of list.children) {
      const inBand = typeof value === 'number' && value >= Number(li.dataset.min) && value <= Number(li.dataset.max);
      li.classList.toggle('selected', inBand);
    }
  }

  _footer() {
    this.statusEl = Dom.el('span', { class: 'status', role: 'status' });
    this.progressEl = Dom.el('span', { class: 'progress-text' });
    this.confirmEl = Dom.el('span', { class: 'confirm' });
    this.submitBtn = Dom.el('button', { class: 'btn primary', type: 'button', onclick: () => this._askConfirm(),
                                        text: 'Submit' });
    const bar = Dom.el('div', { class: 'footer-bar' }, this.progressEl, this.statusEl, this.confirmEl, this.submitBtn);
    if (this.submitted) {
      this.submitBtn.hidden = true;
      this.progressEl.textContent = 'Submitted';
    }
    return bar;
  }

  // ---------------------------------------------------------------- state

  _missing() {
    const out = [];
    for (const section of this.form.sections) {
      for (const item of section.items) {
        if (this.answers[section.name][item.id] === undefined) out.push(`${section.name}/${item.id}`);
      }
    }
    return out;
  }

  _updateProgress() {
    if (this.submitted) return;
    const total = this.form.sections.reduce((n, s) => n + s.items.length, 0);
    this.progressEl.textContent = `${total - this._missing().length} of ${total} answered`;
  }

  _changed() {
    if (this.submitted) return;
    this.dirty = true;
    this._updateProgress();
    this._setStatus('Unsaved changes');
    this._cancelConfirm();
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this._flush(false), FormView.AUTOSAVE_DELAY_MS);
  }

  _setStatus(text, isError = false) {
    this.statusEl.textContent = text;
    this.statusEl.classList.toggle('error', isError);
  }

  _payload() {
    return { form: this.form.kind, subject: this.subjectKey, answers: this.answers };
  }

  async _flush(leaving) {
    clearTimeout(this._timer);
    if (!this.dirty || this.submitted) return true;
    if (this.saving && !leaving) { this._timer = setTimeout(() => this._flush(false), 400); return false; }
    this.saving = true;
    this._setStatus('Saving…');
    try {
      await this.api.request('PUT', '/api/rating', this._payload(), { keepalive: leaving });
      this.dirty = false;
      this._setStatus(`Saved ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`);
      return true;
    } catch (e) {
      this._setStatus(e instanceof ApiError && e.status === 409 ? e.message : `Not saved: ${e.message} Will retry.`, true);
      if (!(e instanceof ApiError) || e.status === 0) this._timer = setTimeout(() => this._flush(false), 5000);
      return false;
    } finally {
      this.saving = false;
    }
  }

  // ---------------------------------------------------------------- submit

  _askConfirm() {
    const missing = this._missing();
    if (missing.length) {
      for (const [key, box] of this._itemBoxes) box.classList.toggle('missing', missing.includes(key));
      const first = this._itemBoxes.get(missing[0]);
      first.scrollIntoView({ behavior: 'smooth', block: 'center' });
      this._setStatus(`${missing.length} question${missing.length > 1 ? 's' : ''} still unanswered`, true);
      return;
    }
    Dom.clear(this.confirmEl);
    this.confirmEl.append(
      Dom.el('span', { text: 'Submitting is final. ' }),
      Dom.el('button', { class: 'btn danger', type: 'button', text: 'Yes, submit', onclick: () => this._submit() }),
      Dom.el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => this._cancelConfirm() }));
    this.submitBtn.hidden = true;
  }

  _cancelConfirm() {
    if (this.submitted) return;
    Dom.clear(this.confirmEl);
    this.submitBtn.hidden = false;
  }

  async _submit() {
    this._setStatus('Submitting…');
    try {
      await this.api.request('POST', '/api/rating/submit', this._payload());
    } catch (e) {
      this._setStatus(e.message, true);
      this._cancelConfirm();
      return;
    }
    this.submitted = true;
    this.dirty = false;
    clearTimeout(this._timer);
    this.root.querySelectorAll('input, textarea').forEach(n => { n.disabled = true; });
    Dom.clear(this.confirmEl);
    this.progressEl.textContent = 'Submitted';
    this._setStatus('Thank you. Your rating is saved.');
    const isSession = this.form.kind === 'session_review';
    this.confirmEl.append(
      isSession ? Dom.el('button', { class: 'btn primary', type: 'button', text: 'Next session', onclick: () => this.onDone('next') }) : null,
      Dom.el('button', { class: 'btn', type: 'button', text: 'Back to start', onclick: () => this.onDone('home') }));
  }
}
