'use strict';
/*
 * The dashboard page. Draws the one JSON document that /api/data returns.
 * All labels from the data are put into the page with textContent.
 */

class Fmt {
  static int(n) { return n == null ? '–' : new Intl.NumberFormat('en-US').format(n); }
  static compact(n) { return n >= 1000 ? d3.format('.2~s')(n) : String(n); }
  static pct(x) { return x == null ? '–' : Math.round(x * 100) + '%'; }
  static secs(x) { return x == null ? '–' : x.toFixed(1); }
  static when(iso) { return iso ? iso.replace('T', ' ').slice(0, 16) + ' UTC' : 'no data yet'; }
}

class Tooltip {
  constructor(el) { this.el = el; }

  /** rows: [{name, value, color}] ; title: string */
  show(x, y, title, rows) {
    const el = this.el;
    el.replaceChildren();
    if (title) {
      const t = document.createElement('div');
      t.className = 't-title';
      t.textContent = title;
      el.append(t);
    }
    for (const r of rows) {
      const row = document.createElement('div');
      row.className = 't-row';
      if (r.color) {
        const key = document.createElement('span');
        key.className = 't-key';
        key.style.borderTopColor = r.color;
        row.append(key);
      }
      const v = document.createElement('span');
      v.className = 't-val';
      v.textContent = r.value;
      const n = document.createElement('span');
      n.className = 't-name';
      n.textContent = r.name;
      row.append(v, n);
      el.append(row);
    }
    el.hidden = false;
    const box = el.getBoundingClientRect();
    el.style.left = Math.max(8, Math.min(x + 14, window.innerWidth - box.width - 8)) + 'px';
    el.style.top = Math.max(8, Math.min(y + 14, window.innerHeight - box.height - 8)) + 'px';
  }

  showAtElement(node, title, rows) {
    const b = node.getBoundingClientRect();
    this.show(b.left + b.width / 2, b.top + b.height / 2, title, rows);
  }

  hide() { this.el.hidden = true; }
}

class Colors {
  static SLOTS = 8;
  static OTHER = 'var(--other)';
  static PRACTICE = 'var(--muted)';
  static PRIMARY = 'var(--series-1)';

  constructor() { this.slotOf = new Map(); }

  /** A pack keeps its color for the life of the page, whatever the filter. */
  forPack(name) {
    if (name === '(none)') return Colors.OTHER;
    if (!this.slotOf.has(name)) this.slotOf.set(name, this.slotOf.size + 1);
    const slot = this.slotOf.get(name);
    return slot <= Colors.SLOTS ? `var(--series-${slot})` : Colors.OTHER;
  }
}

class Card {
  /** A card with a chart and a table view of the same rows. */
  constructor(id, tooltip) {
    this.root = document.getElementById(id);
    this.area = this.root.querySelector('.chart-area');
    this.tooltip = tooltip;
    this.showTable = false;
    this.button = document.createElement('button');
    this.button.type = 'button';
    this.button.textContent = 'Table';
    this.button.setAttribute('aria-pressed', 'false');
    this.button.addEventListener('click', () => {
      this.showTable = !this.showTable;
      this.button.setAttribute('aria-pressed', String(this.showTable));
      this.button.textContent = this.showTable ? 'Chart' : 'Table';
      this.redraw();
    });
    this.root.querySelector('header').append(this.button);
    this.draw = null;       // (area) -> void, set by render()
    this.table = null;      // {columns: [...], rows: [[...]]}
  }

  render(draw, table, toggle = true) {
    this.draw = draw;
    this.table = table;
    this.toggle = toggle;
    this.redraw();
  }

  redraw() {
    this.area.replaceChildren();
    this.tooltip.hide();
    if (!this.table || this.table.rows.length === 0) {
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = 'No data yet.';
      this.area.append(p);
      this.button.hidden = true;
      return;
    }
    this.button.hidden = !this.toggle;
    if (this.showTable && this.toggle) this.drawTable(); else this.draw(this.area);
  }

  drawTable() {
    const t = document.createElement('table');
    t.className = 'data';
    const head = t.createTHead().insertRow();
    for (const c of this.table.columns) {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = c;
      head.append(th);
    }
    const body = t.createTBody();
    for (const r of this.table.rows) {
      const tr = body.insertRow();
      for (const cell of r) tr.insertCell().textContent = cell == null ? '–' : String(cell);
    }
    this.area.append(t);
  }
}

class Shapes {
  /** Rect with a 4px rounded data end on top, square at the baseline. */
  static roundedTop(x, y, w, h, r = 4) {
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  /** Rect with a 4px rounded right end, square at the left baseline. */
  static roundedRight(x, y, w, h, r = 4) {
    r = Math.min(r, h / 2, w);
    return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
  }
  static legend(area, items) {
    if (items.length < 2) return;
    const ul = document.createElement('ul');
    ul.className = 'legend';
    for (const it of items) {
      const li = document.createElement('li');
      const sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.background = it.color;
      li.append(sw, document.createTextNode(it.name));
      ul.append(li);
    }
    area.append(ul);
  }
  static svg(area, width, height, label) {
    return d3.select(area).append('svg')
      .attr('viewBox', `0 0 ${width} ${height}`).attr('role', 'img').attr('aria-label', label);
  }
}

class Dashboard {
  static REFRESH_MS = 5 * 60 * 1000;
  static GAP = 2;               // surface gap between touching marks
  static MAX_BAR = 24;
  static TOP_ORIGINS = 12;

  constructor() {
    this.tooltip = new Tooltip(document.getElementById('tooltip'));
    this.colors = new Colors();
    this.cards = {
      days: new Card('card-days', this.tooltip),
      progress: new Card('card-progress', this.tooltip),
      turns: new Card('card-turns', this.tooltip),
      origin: new Card('card-origin', this.tooltip),
      guess: new Card('card-guess', this.tooltip),
      models: new Card('card-models', this.tooltip),
    };
    this.checkbox = document.getElementById('participants-only');
    this.checkbox.addEventListener('change', () => this.load());
    document.getElementById('refresh').addEventListener('click', () => this.load());
    this.checkboxTouched = false;
    this.checkbox.addEventListener('change', () => { this.checkboxTouched = true; });
    window.addEventListener('resize', () => {
      clearTimeout(this.resizeTimer);
      this.resizeTimer = setTimeout(() => Object.values(this.cards).forEach(c => c.redraw()), 150);
    });
    this.load();
    setInterval(() => this.load(), Dashboard.REFRESH_MS);
  }

  async load() {
    const content = document.getElementById('content');
    content.classList.add('loading');          // keep the old frame, dimmed
    try {
      const only = this.checkbox.checked ? '1' : '0';
      const resp = await fetch(`/api/data?participants_only=${only}`, { cache: 'no-store', credentials: 'same-origin' });
      const doc = await resp.json();
      if (!resp.ok) {
        if (resp.status === 409 && this.checkbox.checked) {   // no participants.json yet: show everyone
          this.checkbox.checked = false;
          this.checkbox.disabled = true;
          document.getElementById('participants-note').textContent =
            'The participant list has not arrived yet; showing everyone.';
          return this.load();
        }
        throw new Error(doc.error || `HTTP ${resp.status}`);
      }
      this.hideBanner();
      this.draw(doc);
    } catch (e) {
      this.showBanner(`Could not load the data: ${e.message}`);
    } finally {
      content.classList.remove('loading');
    }
  }

  showBanner(text) {
    const b = document.getElementById('banner');
    b.textContent = text;
    b.hidden = false;
  }
  hideBanner() { document.getElementById('banner').hidden = true; }

  draw(doc) {
    const m = doc.meta;
    document.getElementById('subtitle').textContent =
      `Data through ${Fmt.when(m.data_through)} · ${m.files.length} log file${m.files.length === 1 ? '' : 's'}`;
    this.checkbox.disabled = !m.participants_available;
    if (m.participants_available) {
      document.getElementById('participants-note').textContent = m.participants_only
        ? `${Fmt.int(m.participants_listed)} participants listed` : 'Includes test accounts and anyone not listed.';
    }
    this.drawKpis(doc);
    this.drawDays(doc);
    this.drawProgress(doc);
    this.drawTurns(doc);
    this.drawOrigin(doc);
    this.drawGuess(doc);
    this.drawModels(doc);
    this.drawNotes(doc);
  }

  // ------------------------------------------------------------- KPIs

  drawKpis(doc) {
    const k = doc.kpis, m = doc.meta;
    const started = m.participants_listed != null
      ? `${Fmt.int(k.participants_started)} of ${Fmt.int(m.participants_listed)}` : Fmt.int(k.participants_started);
    const tiles = [
      { label: `Cases past the ${m.practice_cases} practice cases`, value: Fmt.int(k.real_cases), hero: true,
        sub: `${Fmt.int(k.cases)} cases opened in all` },
      { label: 'Participants started', value: started },
      { label: 'Student turns', value: Fmt.int(k.student_turns) },
      { label: 'Defense guesses right', value: Fmt.pct(k.guess_accuracy), sub: `of ${Fmt.int(k.guesses)} guesses` },
      { label: 'Session critiques', value: Fmt.int(k.critiques_ok),
        sub: `${Fmt.int(k.critiques_failed)} failed, ${Fmt.int(k.critiques_unanswered)} unanswered` },
    ];
    const host = document.getElementById('kpis');
    host.replaceChildren();
    for (const t of tiles) {
      const d = document.createElement('div');
      d.className = 'tile' + (t.hero ? ' hero' : '');
      for (const [cls, text] of [['label', t.label], ['value', t.value], ['sub', t.sub]]) {
        if (text == null) continue;
        const e = document.createElement('div');
        e.className = cls;
        e.textContent = text;
        d.append(e);
      }
      host.append(d);
    }
  }

  // ------------------------------------------------- cases per day

  drawDays(doc) {
    const packs = doc.packs;
    const days = this.dayRange(doc.cases_by_day);
    const byDay = d3.group(doc.cases_by_day, r => r.day);
    const color = p => this.colors.forPack(p);
    const totals = days.map(d => d3.sum(byDay.get(d) || [], r => r.n));
    this.cards.days.render(area => {
      Shapes.legend(area, packs.map(p => ({ name: p, color: color(p) })));
      const W = area.clientWidth || 800, H = 260, m = { l: 36, r: 8, t: 8, b: 28 };
      const svg = Shapes.svg(area, W, H, 'Cases opened per day, stacked by experiment pack');
      const x = d3.scaleBand(days, [m.l, W - m.r]).paddingInner(0.15);
      const y = d3.scaleLinear([0, Math.max(1, d3.max(totals))], [H - m.b, m.t]).nice();
      svg.append('g').selectAll('line').data(y.ticks(4)).join('line').attr('class', 'grid-line')
        .attr('x1', m.l).attr('x2', W - m.r).attr('y1', d => y(d)).attr('y2', d => y(d));
      svg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`)
        .call(d3.axisLeft(y).ticks(4).tickFormat(Fmt.compact).tickSize(0).tickPadding(8)).select('.domain').remove();
      const every = Math.ceil(days.length / Math.max(1, Math.floor((W - m.l) / 70)));
      svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`)
        .call(d3.axisBottom(x).tickValues(days.filter((_, i) => i % every === 0))
          .tickFormat(d => d.slice(5)).tickSize(0).tickPadding(8));
      const bw = Math.min(Dashboard.MAX_BAR, x.bandwidth());
      days.forEach((day, i) => {
        const x0 = x(day) + (x.bandwidth() - bw) / 2;
        let top = y(0);
        const segs = (byDay.get(day) || []).slice().sort((a, b) => packs.indexOf(a.pack) - packs.indexOf(b.pack));
        segs.forEach((s, j) => {
          const h = Math.max(1, y(0) - y(s.n) - (j < segs.length - 1 ? Dashboard.GAP : 0));
          const yTop = top - h;
          const last = j === segs.length - 1;
          svg.append('path').attr('fill', color(s.pack))
            .attr('d', last ? Shapes.roundedTop(x0, yTop, bw, h) : `M${x0},${yTop}h${bw}v${h}h${-bw}Z`);
          top = yTop - Dashboard.GAP;
        });
        const hit = svg.append('rect').attr('class', 'hit').attr('tabindex', 0)
          .attr('x', x(day)).attr('width', x.bandwidth()).attr('y', m.t).attr('height', H - m.t - m.b)
          .attr('aria-label', `${day}: ${totals[i]} cases`);
        const rows = segs.map(s => ({ name: s.pack, value: Fmt.int(s.n), color: color(s.pack) }));
        hit.on('pointermove', e => this.tooltip.show(e.clientX, e.clientY, day, rows))
          .on('pointerleave blur', () => this.tooltip.hide())
          .on('focus', e => this.tooltip.showAtElement(e.target, day, rows));
      });
    }, {
      columns: ['Day (UTC)', ...packs, 'Total'],
      rows: days.map((d, i) => [d, ...packs.map(p => ((byDay.get(d) || []).find(r => r.pack === p) || { n: 0 }).n), totals[i]]),
    });
  }

  dayRange(rows) {
    if (!rows.length) return [];
    const days = rows.map(r => r.day).sort();
    const out = [];
    for (let t = new Date(days[0] + 'T00:00:00Z'); t <= new Date(days[days.length - 1] + 'T00:00:00Z');
         t = new Date(t.getTime() + 86400000)) out.push(t.toISOString().slice(0, 10));
    return out;
  }

  // ---------------------------------------------- horizontal bars

  /** Shared drawing for the row-per-item charts. segments: [{key, value, color}] stack left to right. */
  hbars(area, rows, opts) {
    const rowH = opts.rowH || 24, m = { l: opts.labelW, r: 56, t: 4, b: 4 };
    const W = area.clientWidth || 480, H = rows.length * rowH + m.t + m.b;
    const svg = Shapes.svg(area, W, H, opts.label);
    const max = opts.max != null ? opts.max : Math.max(1, d3.max(rows, r => d3.sum(r.segments, s => s.value)));
    const x = d3.scaleLinear([0, max], [m.l, W - m.r]);
    const bh = Math.min(Dashboard.MAX_BAR, rowH - 8);
    rows.forEach((r, i) => {
      const yy = m.t + i * rowH + (rowH - bh) / 2;
      svg.append('text').attr('class', 'row-label').attr('x', m.l - 8).attr('y', yy + bh / 2)
        .attr('dy', '0.35em').attr('text-anchor', 'end').text(r.label);
      let cursor = m.l;
      const live = r.segments.filter(s => s.value > 0);
      live.forEach((s, j) => {
        const w = Math.max(1, x(s.value) - x(0) - (j < live.length - 1 ? Dashboard.GAP : 0));
        const last = j === live.length - 1;
        svg.append('path').attr('fill', s.color)
          .attr('d', last ? Shapes.roundedRight(cursor, yy, w, bh) : `M${cursor},${yy}h${w}v${bh}h${-w}Z`);
        cursor += w + Dashboard.GAP;
      });
      svg.append('text').attr('class', 'value').attr('x', Math.max(cursor, m.l) + 6).attr('y', yy + bh / 2)
        .attr('dy', '0.35em').text(r.tip);
      const hit = svg.append('rect').attr('class', 'hit').attr('tabindex', 0).attr('x', 0).attr('width', W)
        .attr('y', m.t + i * rowH).attr('height', rowH).attr('aria-label', `${r.label}: ${r.tip}`);
      hit.on('pointermove', e => this.tooltip.show(e.clientX, e.clientY, r.label, r.details))
        .on('pointerleave blur', () => this.tooltip.hide())
        .on('focus', e => this.tooltip.showAtElement(e.target, r.label, r.details));
    });
    svg.append('line').attr('class', 'grid-line').attr('x1', m.l).attr('x2', m.l).attr('y1', m.t).attr('y2', H - m.b)
      .style('stroke', 'var(--axis)');
  }

  drawProgress(doc) {
    const practiceN = doc.meta.practice_cases;
    const rows = doc.progress.map(p => ({
      label: p.id, tip: Fmt.int(p.cases),
      segments: [{ value: Math.min(p.cases, practiceN), color: Colors.PRACTICE },
                 { value: p.real_cases, color: Colors.PRIMARY }],
      details: [{ name: 'cases opened', value: Fmt.int(p.cases) },
                { name: 'past practice', value: Fmt.int(p.real_cases), color: 'var(--series-1)' },
                { name: 'active days', value: Fmt.int(p.active_days) },
                { name: `pack ${p.pack}`, value: '' },
                { name: 'last active', value: Fmt.when(p.last_active) }],
    }));
    this.cards.progress.render(area => {
      Shapes.legend(area, [{ name: 'Practice cases', color: Colors.PRACTICE }, { name: 'Past practice', color: Colors.PRIMARY }]);
      this.hbars(area, rows, { labelW: 84, label: 'Cases opened per participant, practice cases and later cases' });
    }, {
      columns: ['Participant', 'Pack', 'Cases', 'Past practice', 'Active days', 'Last active (UTC)'],
      rows: doc.progress.map(p => [p.id, p.pack, p.cases, p.real_cases, p.active_days, Fmt.when(p.last_active)]),
    });
  }

  drawOrigin(doc) {
    const all = doc.origin.slice().sort((a, b) => b.students - a.students || b.cases - a.cases || a.tz.localeCompare(b.tz));
    const top = all.slice(0, Dashboard.TOP_ORIGINS);
    const rest = all.slice(Dashboard.TOP_ORIGINS);
    const rows = top.map(o => ({ name: o.tz, students: o.students, cases: o.cases }));
    if (rest.length) rows.push({ name: `Other (${rest.length} zones)`, students: d3.sum(rest, o => o.students),
                                 cases: d3.sum(rest, o => o.cases) });
    this.cards.origin.render(area => {
      this.hbars(area, rows.map(r => ({
        label: r.name, tip: Fmt.int(r.students),
        segments: [{ value: r.students, color: Colors.PRIMARY }],
        details: [{ name: 'students', value: Fmt.int(r.students), color: 'var(--series-1)' },
                  { name: 'cases', value: Fmt.int(r.cases) }],
      })), { labelW: 150, label: 'Students by session timezone' });
    }, { columns: ['Timezone', 'Area', 'Students', 'Cases'], rows: all.map(o => [o.tz, o.area, o.students, o.cases]) });
  }

  drawGuess(doc) {
    const rows = doc.guess_by_defense;
    this.cards.guess.render(area => {
      this.hbars(area, rows.map(r => ({
        label: r.defense, tip: `${Fmt.pct(r.correct / r.n)}`,
        segments: [{ value: r.correct / r.n, color: Colors.PRIMARY }],
        details: [{ name: 'guessed right', value: `${r.correct} of ${r.n}`, color: 'var(--series-1)' }],
      })), { labelW: 150, max: 1, label: 'Share of defense guesses that were right, by defense' });
    }, { columns: ['Defense', 'Guesses', 'Right', 'Share'], rows: rows.map(r => [r.defense, r.n, r.correct, Fmt.pct(r.correct / r.n)]) });
  }

  // ---------------------------------------------------- histogram

  drawTurns(doc) {
    const bins = doc.turn_length;
    const total = d3.sum(bins, b => b.n);
    this.cards.turns.render(area => {
      const W = area.clientWidth || 480, H = 240, m = { l: 36, r: 8, t: 18, b: 28 };
      const svg = Shapes.svg(area, W, H, 'Number of student turns by length in words');
      const x = d3.scaleBand(bins.map(b => b.label), [m.l, W - m.r]).paddingInner(0.2);
      const y = d3.scaleLinear([0, Math.max(1, d3.max(bins, b => b.n))], [H - m.b, m.t]).nice();
      svg.append('g').selectAll('line').data(y.ticks(4)).join('line').attr('class', 'grid-line')
        .attr('x1', m.l).attr('x2', W - m.r).attr('y1', d => y(d)).attr('y2', d => y(d));
      svg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`)
        .call(d3.axisLeft(y).ticks(4).tickFormat(Fmt.compact).tickSize(0).tickPadding(8)).select('.domain').remove();
      svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`)
        .call(d3.axisBottom(x).tickSize(0).tickPadding(8));
      const bw = Math.min(Dashboard.MAX_BAR * 2, x.bandwidth());
      for (const b of bins) {
        const x0 = x(b.label) + (x.bandwidth() - bw) / 2, h = y(0) - y(b.n);
        if (b.n > 0) {
          svg.append('path').attr('fill', Colors.PRIMARY).attr('d', Shapes.roundedTop(x0, y(b.n), bw, Math.max(1, h)));
          svg.append('text').attr('class', 'value').attr('x', x0 + bw / 2).attr('y', y(b.n) - 5)
            .attr('text-anchor', 'middle').text(Fmt.int(b.n));
        }
        const rows = [{ name: 'turns', value: Fmt.int(b.n), color: 'var(--series-1)' },
                      { name: 'of all', value: total ? Fmt.pct(b.n / total) : '–' }];
        svg.append('rect').attr('class', 'hit').attr('tabindex', 0).attr('x', x(b.label)).attr('width', x.bandwidth())
          .attr('y', m.t).attr('height', H - m.t - m.b).attr('aria-label', `${b.label} words: ${b.n} turns`)
          .on('pointermove', e => this.tooltip.show(e.clientX, e.clientY, `${b.label} words`, rows))
          .on('pointerleave blur', () => this.tooltip.hide())
          .on('focus', e => this.tooltip.showAtElement(e.target, `${b.label} words`, rows));
      }
    }, { columns: ['Words per turn', 'Turns'], rows: total ? bins.map(b => [b.label, b.n]) : [] });
  }

  // ------------------------------------------------------ models

  drawModels(doc) {
    const columns = ['Model', 'Flow', 'Count', 'Mean', 'Std', 'Min', 'Max'];
    const rows = doc.models.map(r => [r.model, r.action, r.count, Fmt.secs(r.mean), Fmt.secs(r.std),
                                      Fmt.secs(r.min), Fmt.secs(r.max)]);
    this.cards.models.render(area => {
      const t = document.createElement('table');
      t.className = 'data';
      const head = t.createTHead().insertRow();
      for (const c of columns) {
        const th = document.createElement('th');
        th.scope = 'col';
        th.textContent = c;
        head.append(th);
      }
      const body = t.createTBody();
      for (const r of rows) {
        const tr = body.insertRow();
        for (const cell of r) tr.insertCell().textContent = String(cell);
      }
      area.append(t);
    }, { columns, rows }, false);
  }

  // -------------------------------------------------------- notes

  drawNotes(doc) {
    const host = document.getElementById('notes');
    host.replaceChildren();
    const lines = [...doc.meta.notes];
    if (doc.meta.unrecognized_lines) lines.push(`WARNING: ${doc.meta.unrecognized_lines} content log line(s) could not be classified.`);
    lines.push(`Generated ${Fmt.when(doc.meta.generated_at)}. The page refreshes itself every five minutes.`);
    for (const l of lines) {
      const p = document.createElement('p');
      p.textContent = l;
      host.append(p);
    }
  }
}

new Dashboard();
