/**
 * **********************************************************
 * @Author: Andreas Paepcke
 * @Date:   2026-10-11 16:46:16
 * @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/static/app.mjs
 * @Last Modified by:   Andreas Paepcke
 * @Last Modified time: 2026-10-11 16:46:16
 * **********************************************************
 */

'use strict';

/*
 * The page: a small hash router and the views for logging in, the start page,
 * the rating forms and the administrator's progress table.
 */

import { Api, ApiError } from './api.mjs';
import { Dom } from './dom.mjs';
import { FormView } from './forms.mjs';

class App {
  constructor() {
    this.api = new Api();
    this.main = document.getElementById('app');
    this.topbar = document.getElementById('topbar');
    this.nav = document.getElementById('nav');
    this.me = null;
    this.formView = null;
    this.api.onLoggedOut = () => { this.me = null; this.go('/login'); };
    window.addEventListener('hashchange', () => this.route());
  }

  go(path) {
    if (location.hash === `#${path}`) this.route();
    else location.hash = path;
  }

  async start() {
    if (this.api.token) {
      try {
        const doc = await this.api.request('GET', '/api/me');
        this.me = doc.me;
      } catch (e) { /* the login view will show */ }
    }
    this.route();
  }

  // ---------------------------------------------------------------- routing

  async route() {
    if (this.formView) { await this.formView.dispose(); this.formView = null; }
    const parts = (location.hash.replace(/^#/, '') || '/').split('/').filter(Boolean).map(decodeURIComponent);
    this._renderNav();
    if (!this.me) {
      return this._show(parts[0] === 'register' ? new RegisterView(this) : new LoginView(this));
    }
    const [head, form, subject] = parts;
    try {
      if (head === 'review') return await this._reviewNext();
      if (head === 'item' && form && subject) return await this._item(form, subject);
      if (head === 'admin' && this.me.is_admin) return this._show(new AdminView(this));
      return this._show(new HomeView(this));
    } catch (e) {
      this._error(e);
    }
  }

  _renderNav() {
    Dom.clear(this.nav);
    this.topbar.hidden = !this.me;
    if (!this.me) return;
    this.nav.append(Dom.el('a', { href: '#/', text: 'Start' }));
    if (this.me.is_admin) this.nav.append(Dom.el('a', { href: '#/admin', text: 'Progress' }));
    this.nav.append(Dom.el('span', { class: 'who', text: this.me.label }));
    this.nav.append(Dom.el('button', { class: 'btn link', type: 'button', text: 'Log out', onclick: async () => {
      await this.api.logout();
      this.me = null;
      this.go('/login');
    } }));
  }

  _show(view) {
    Dom.clear(this.main);
    this.main.append(view.root);
    if (view.load) view.load().catch(e => this._error(e));
    window.scrollTo(0, 0);
  }

  _error(e) {
    Dom.clear(this.main);
    this.main.append(Dom.el('p', { class: 'notice bad', text: e.message || 'Something went wrong.' }),
                     Dom.el('p', {}, Dom.el('a', { href: '#/', text: 'Back to the start page' })));
  }

  async _reviewNext() {
    const { subject } = await this.api.request('GET', '/api/review/next');
    if (subject === null) {
      Dom.clear(this.main);
      this.main.append(Dom.el('h1', { text: 'All done' }),
        Dom.el('p', { text: 'You have rated every session that is available right now. Thank you! New sessions may be added later.' }),
        Dom.el('p', {}, Dom.el('a', { class: 'btn', href: '#/', text: 'Back to the start page' })));
      return;
    }
    location.replace(`#/item/session_review/${encodeURIComponent(subject)}`);
  }

  async _item(form, subject) {
    const data = await this.api.request('GET', `/api/item?form=${encodeURIComponent(form)}&subject=${encodeURIComponent(subject)}`);
    this.formView = new FormView(this.api, data, where => this.go(where === 'next' ? '/review' : '/'));
    Dom.clear(this.main);
    this.main.append(this.formView.root);
    window.scrollTo(0, 0);
  }
}

class LoginView {
  constructor(app) {
    this.app = app;
    this.email = Dom.el('input', { type: 'email', id: 'email', autocomplete: 'username', required: true, autofocus: true });
    this.password = Dom.el('input', { type: 'password', id: 'password', autocomplete: 'current-password', required: true });
    this.message = Dom.el('p', { class: 'notice bad', hidden: true, role: 'alert' });
    const form = Dom.el('form', { class: 'card narrow', onsubmit: e => { e.preventDefault(); this._submit(); } },
      Dom.el('h1', { text: 'Log in' }),
      Dom.el('label', { for: 'email', text: 'E-mail address' }), this.email,
      Dom.el('label', { for: 'password', text: 'Password' }), this.password,
      this.message,
      Dom.el('button', { class: 'btn primary', type: 'submit', text: 'Log in' }),
      Dom.el('p', { class: 'muted' }, 'First visit? ', Dom.el('a', { href: '#/register', text: 'Use your invite code' })));
    this.root = Dom.el('div', {}, form);
  }

  async _submit() {
    this.message.hidden = true;
    try {
      this.app.me = await this.app.api.login(this.email.value, this.password.value);
      this.app.go('/');
    } catch (e) {
      this.message.textContent = e.message;
      this.message.hidden = false;
    }
  }
}

class RegisterView {
  constructor(app) {
    this.app = app;
    const field = (label, id, attrs) => [Dom.el('label', { for: id, text: label }), Dom.el('input', { id, required: true, ...attrs })];
    this.fields = {
      email: field('E-mail address', 'email', { type: 'email', autocomplete: 'username' }),
      code: field('Invite code', 'code', { type: 'text', autocomplete: 'one-time-code', autocapitalize: 'characters', spellcheck: 'false' }),
      password: field('Choose a password (at least 10 characters)', 'password', { type: 'password', autocomplete: 'new-password', minlength: 10 }),
      again: field('Password again', 'again', { type: 'password', autocomplete: 'new-password', minlength: 10 }),
    };
    this.message = Dom.el('p', { class: 'notice bad', hidden: true, role: 'alert' });
    const form = Dom.el('form', { class: 'card narrow', onsubmit: e => { e.preventDefault(); this._submit(); } },
      Dom.el('h1', { text: 'First visit' }),
      Dom.el('p', { class: 'muted', text: 'Enter the e-mail address and invite code you were given, and choose a password. This browser will then remember you.' }),
      Object.values(this.fields).flat(), this.message,
      Dom.el('button', { class: 'btn primary', type: 'submit', text: 'Create my login' }),
      Dom.el('p', { class: 'muted' }, Dom.el('a', { href: '#/login', text: 'I already have a password' })));
    this.root = Dom.el('div', {}, form);
  }

  async _submit() {
    const v = name => this.fields[name][1].value;
    this.message.hidden = true;
    if (v('password') !== v('again')) {
      this.message.textContent = 'The two passwords differ.';
      this.message.hidden = false;
      return;
    }
    try {
      this.app.me = await this.app.api.register(v('email'), v('code'), v('password'));
      this.app.go('/');
    } catch (e) {
      this.message.textContent = e.message;
      this.message.hidden = false;
    }
  }
}

class HomeView {
  constructor(app) {
    this.app = app;
    this.root = Dom.el('div', {}, Dom.el('p', { class: 'muted', text: 'Loading…' }));
  }

  async load() {
    const { me, overview, bundle } = await this.app.api.request('GET', '/api/me');
    const root = Dom.clear(this.root);
    root.append(Dom.el('h1', { text: `Hello${me.name ? ', ' + me.name : ''}` }));
    let anything = false;

    if (overview.assessments.length) {
      anything = true;
      root.append(Dom.el('h2', { text: 'Assessments' }));
      const list = Dom.el('ul', { class: 'tasks' });
      for (const task of overview.assessments) {
        list.append(Dom.el('li', {},
          Dom.el('span', { class: 'task-title', text: task.title }),
          Dom.el('span', { class: `chip ${task.state}`, text: { new: 'to do', draft: 'draft', submitted: 'submitted' }[task.state] }),
          Dom.el('a', { class: 'btn', href: `#/item/${task.form}/${encodeURIComponent(task.subject)}`,
                        text: task.state === 'submitted' ? 'View' : (task.state === 'draft' ? 'Continue' : 'Start') })));
      }
      root.append(list);
    }

    const reviews = overview.reviews;
    if (reviews) {
      anything = true;
      root.append(Dom.el('h2', { text: 'Session reviews' }));
      if (reviews.available === 0) {
        root.append(Dom.el('p', { class: 'muted', text: 'No sessions are available yet. Please check back later.' }));
      } else {
        const done = reviews.submitted, total = reviews.available;
        root.append(
          Dom.el('p', { text: `${done} of ${total} sessions rated` + (reviews.draft ? `, ${reviews.draft} unfinished` : '') + '.' }),
          Dom.el('progress', { max: total, value: done, 'aria-label': 'Sessions rated' }),
          Dom.el('p', {}, reviews.remaining
            ? Dom.el('a', { class: 'btn primary', href: '#/review', text: reviews.draft ? 'Continue reviewing' : 'Review a session' })
            : Dom.el('span', { class: 'muted', text: 'You have rated all available sessions. Thank you!' })),
          Dom.el('p', { class: 'muted', text: 'Sessions come in random order and you can stop at any time. Unfinished ratings are saved.' }));
      }
    }
    if (!anything) {
      root.append(Dom.el('p', { class: 'muted', text: me.is_admin
        ? 'Nothing is assigned to this account. Use the Progress page.'
        : 'Nothing is waiting for you right now.' }));
    }
  }
}

class AdminView {
  constructor(app) {
    this.app = app;
    this.onlyReminders = false;
    this.root = Dom.el('div', {}, Dom.el('p', { class: 'muted', text: 'Loading…' }));
  }

  async load() {
    this.doc = await this.app.api.request('GET', '/api/admin/progress');
    this._render();
  }

  _render() {
    const doc = this.doc;
    const root = Dom.clear(this.root);
    root.append(Dom.el('h1', { text: 'Progress' }));
    const b = doc.bundle;
    root.append(Dom.el('p', { class: 'muted', text: b
      ? `Sessions bundle ${b.bundle_id}: ${b.session_count} sessions of ${b.participant_count} students.`
      : 'No sessions bundle has arrived yet.' }));
    const rows = doc.rows.filter(r => !this.onlyReminders || r.needs_reminder);

    const reminders = doc.rows.filter(r => r.needs_reminder);
    this.copyStatus = Dom.el('span', { class: 'muted', role: 'status' });
    root.append(Dom.el('div', { class: 'toolbar' },
      Dom.el('label', { class: 'inline' },
        Dom.el('input', { type: 'checkbox', checked: this.onlyReminders, onchange: e => { this.onlyReminders = e.target.checked; this._render(); } }),
        ` Only those who need a reminder (${reminders.length})`),
      Dom.el('button', { class: 'btn', type: 'button', text: 'Copy their e-mail addresses', onclick: () => this._copy(reminders) }),
      Dom.el('button', { class: 'btn', type: 'button', text: 'Download all ratings (CSV)', onclick: () => this._download() }),
      this.copyStatus));

    const table = Dom.el('table', { class: 'progress' });
    table.append(Dom.el('thead', {}, Dom.el('tr', {},
      ...['', 'Name / e-mail', 'Roles', 'Session reviews', 'Assessments', 'Last active'].map(h => Dom.el('th', { scope: 'col', text: h })))));
    const body = Dom.el('tbody');
    for (const r of rows) body.append(this._row(r));
    if (!rows.length) body.append(Dom.el('tr', {}, Dom.el('td', { colspan: 6, class: 'muted', text: 'Nobody to show.' })));
    table.append(body);
    root.append(Dom.el('div', { class: 'table-wrap' }, table));
  }

  _row(r) {
    const reviews = r.reviews
      ? `${r.reviews.submitted} / ${r.reviews.available}` + (r.reviews.draft ? ` (+${r.reviews.draft} draft)` : '')
      : '–';
    const assess = r.assessments.expected ? `${r.assessments.submitted} / ${r.assessments.expected}` +
      (r.assessments.draft ? ` (+${r.assessments.draft} draft)` : '') : '–';
    let last = 'never';
    if (r.last_seen_utc) last = r.days_idle === 0 ? 'today' : `${r.days_idle} day${r.days_idle === 1 ? '' : 's'} ago`;
    const flag = r.disabled ? Dom.el('span', { class: 'chip disabled', text: 'disabled' })
      : (!r.activated ? Dom.el('span', { class: 'chip bad', text: r.invite_pending ? 'not yet logged in' : 'no password' })
        : (r.needs_reminder ? Dom.el('span', { class: 'chip bad', text: 'remind' }) : Dom.el('span', { class: 'chip submitted', text: 'ok' })));
    return Dom.el('tr', {},
      Dom.el('td', {}, flag),
      Dom.el('td', {}, Dom.el('strong', { text: r.label }), Dom.el('br'), r.name ? `${r.name} ` : '', Dom.el('span', { class: 'muted', text: r.email })),
      Dom.el('td', { text: r.roles.join(', ') }),
      Dom.el('td', { text: reviews }),
      Dom.el('td', { text: assess }),
      Dom.el('td', { text: last }));
  }

  async _copy(rows) {
    const text = rows.map(r => r.email).join(', ');
    try {
      await navigator.clipboard.writeText(text);
      this.copyStatus.textContent = rows.length ? `Copied ${rows.length} address${rows.length > 1 ? 'es' : ''}.` : 'Nobody needs a reminder.';
    } catch (e) {
      window.prompt('Copy these addresses:', text);   // falls back where the clipboard is blocked
    }
  }

  async _download() {
    try {
      const blob = await this.app.api.exportCsv();
      const link = Dom.el('a', { href: URL.createObjectURL(blob), download: 'ratings.csv' });
      document.body.append(link);
      link.click();
      link.remove();
    } catch (e) {
      this.copyStatus.textContent = e.message;
    }
  }
}

new App().start();
