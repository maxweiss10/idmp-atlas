/* IDMP Atlas client. No dependencies. Modules: lens, modals, palette (search), chooser, bands, drawer, recents, filters, explorer, offline. */
(function () {
  'use strict';
  var html = document.documentElement, root = html.getAttribute('data-root') || '', REPO = '__REPO__';
  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function store(k, v) { try { if (v === null || v === undefined) localStorage.removeItem(k); else localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v)); } catch (e) {} }
  function read(k, json) { try { var v = localStorage.getItem(k); return json ? (v ? JSON.parse(v) : null) : v; } catch (e) { return null; } }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function norm(s) { return (s || '').toLowerCase().replace(/[’']/g, '').replace(/[^a-z0-9%\/\-\.\+ ]+/g, ' ').replace(/\s+/g, ' ').trim(); }
  var toastEl = $('#toast'), toastT;
  function toast(msg, ms) { if (!toastEl) return; toastEl.textContent = msg; toastEl.hidden = false; clearTimeout(toastT); toastT = setTimeout(function () { toastEl.hidden = true; }, ms || 1800); }
  function fetchJSON(u) { return fetch(u, { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }); }

  /* ---------- lens (Where / Setting / Patient) ---------- */
  var LENS_KEYS = ['where', 'setting', 'patient', 'renal', 'allergy'];
  var lens = read('lens', true) || {};
  var LABELS = { where: { '': 'All sites', ucsf: 'UCSF Health', zsfg: 'ZSFG', va: 'VA', bch: 'BCH' }, setting: { '': 'Any setting', outpatient: 'Outpatient', inpatient: 'Inpatient', icu: 'ICU' }, patient: { '': 'Adult', peds: 'Pediatric' } };
  function lensSummary() {
    var parts = [LABELS.where[lens.where || ''], LABELS.setting[lens.setting || ''], LABELS.patient[lens.patient || '']];
    if (lens.crcl) parts.push('CrCl ' + lens.crcl); else if (lens.renal) parts.push(lens.renal.toUpperCase());
    if (lens.allergy) parts.push('beta-lactam allergy');
    return parts.join(', ');
  }
  /* The patient lens answers "adult or pediatric". The index has to answer with it:
     an index group for the excluded population is hidden outright, not just de-emphasised,
     so it leaves the tab order and the accessibility tree with it. The one exception is the
     group holding the page you are on, which stays put so the nav never loses your place. */
  function applyIdxPop() {
    var want = lens.patient === 'peds' ? 'pediatric' : 'adult';
    $$('.idx-grp[data-pop]').forEach(function (g) {
      var mine = g.getAttribute('data-pop') === want || !!g.querySelector('a[aria-current="page"]');
      g.hidden = !mine;
    });
    $$('.idx-count').forEach(function (c) {
      var n = c.getAttribute(want === 'pediatric' ? 'data-n-peds' : 'data-n-adult');
      if (n !== null) c.textContent = n;
    });
  }

  function applyLens(save) {
    LENS_KEYS.forEach(function (k) { if (lens[k]) html.setAttribute('data-' + k, lens[k]); else html.removeAttribute('data-' + k); });
    if (save) store('lens', lens);
    var s = lensSummary();
    $$('#lens-summary').forEach(function (el) { el.textContent = s; });
    $$('.seg[data-lens]').forEach(function (seg) {
      var k = seg.getAttribute('data-lens');
      $$('button', seg).forEach(function (b) { b.classList.toggle('on', (b.getAttribute('data-v') || '') === (lens[k] || '')); });
    });
    $$('#lens-crcl, #dial-crcl').forEach(function (i) { if (document.activeElement !== i) i.value = lens.crcl || ''; });
    // where: guideline rows and per-site sections
    $$('li[data-sites]').forEach(function (li) { var sites = (li.getAttribute('data-sites') || '').split(/\s+/); li.classList.toggle('site-match', !lens.where || sites.indexOf(lens.where) >= 0); });
    $$('ul.rel-list, ul.links.gl').forEach(function (ul) {
      var items = $$('li[data-sites]', ul); if (!items.length || !lens.where) return;
      items.sort(function (a, b) { return (b.classList.contains('site-match') ? 1 : 0) - (a.classList.contains('site-match') ? 1 : 0); });
      items.forEach(function (li) { ul.appendChild(li); });
    });
    $$('.site-sec[data-site], .restrict[data-site]').forEach(function (sec) { var s2 = sec.getAttribute('data-site'); sec.classList.toggle('on', !!lens.where && s2 === lens.where); sec.classList.toggle('off', !!lens.where && s2 !== lens.where && s2 !== ''); });
    applyIdxPop();
    applyBands(document); applyDoseStrips(document); ctxAuto();
    document.dispatchEvent(new CustomEvent('lens-change'));
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('.seg[data-lens] button'); if (!b) return;
    var k = b.parentNode.getAttribute('data-lens'); lens[k] = b.getAttribute('data-v') || '';
    if (k === 'renal' && lens.renal) { lens.crcl = ''; }
    applyLens(true);
  });
  $$('#lens-crcl, #dial-crcl').forEach(function (i) { i.addEventListener('input', function () { var v = parseInt(i.value, 10); lens.crcl = isNaN(v) ? '' : String(v); if (lens.crcl) lens.renal = ''; applyLens(true); }); });
  var lensReset = $('#lens-reset'); if (lensReset) lensReset.addEventListener('click', function () { lens = {}; applyLens(true); });
  var params = new URLSearchParams(location.search);
  if (params.get('site')) { lens.where = groupOf(params.get('site')); }
  function groupOf(slug) { slug = (slug || '').toLowerCase(); if (/zuckerberg|zsfg/.test(slug)) return 'zsfg'; if (/veteran|^va/.test(slug)) return 'va'; if (/benioff|children|bch|oak/.test(slug)) return 'bch'; return 'ucsf'; }

  /* ---------- renal bands and dose strips ---------- */
  function bandMatches(th) {
    var kind = th.getAttribute('data-kind');
    if (kind === 'hd') return lens.renal === 'hd' || (lens.renal === 'crrt' && th.getAttribute('data-also') === 'crrt');
    if (kind === 'crrt') return lens.renal === 'crrt';
    if (kind !== 'crcl' || !lens.crcl) return false;
    var v = parseInt(lens.crcl, 10), lo = th.getAttribute('data-lo'), hi = th.getAttribute('data-hi');
    var okLo = lo === '' || (th.getAttribute('data-loi') === '1' ? v >= +lo : v > +lo);
    var okHi = hi === '' || (th.getAttribute('data-hii') === '1' ? v <= +hi : v < +hi);
    return okLo && okHi;
  }
  function applyBands(scope) {
    $$('table.bands', scope).forEach(function (t) {
      var on = null;
      $$('th[data-kind]', t).forEach(function (th) { if (on === null && bandMatches(th)) on = th.getAttribute('data-col'); });
      $$('[data-col]', t).forEach(function (c) { c.classList.toggle('on', on !== null && c.getAttribute('data-col') === on); });
    });
  }
  function pickBand(bands) {
    if (!bands || !bands.length) return 0;
    if (lens.crcl) { var v = parseInt(lens.crcl, 10); for (var i = 0; i < bands.length; i++) { var b = bands[i]; var okLo = b.lo == null || (b.loi ? v >= b.lo : v > b.lo), okHi = b.hi == null || (b.hii ? v <= b.hi : v < b.hi); if (okLo && okHi) return i; } }
    return 0;
  }
  /* The server renders the dose line, its marks and the band table. A lens change only
     moves the band, so update the number and the highlight in place and leave marks alone. */
  function applyDoseStrips(scope) {
    var dataEl = $('#dose-data', scope); if (!dataEl) return;
    var data; try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
    $$('.dose[data-dose]', scope).forEach(function (el) {
      var dd = data[el.getAttribute('data-dose')]; if (!dd) return;
      var ri = +(el.getAttribute('data-row') || 0);
      if (ri >= dd.rows.length) ri = 0;
      var bi = lens.renal ? 0 : pickBand(dd.bands);
      var txt = dd.rows[ri].d[bi] || '';
      var d = $('.dl-d', el); if (d && txt) d.textContent = txt;
      var band = dd.bands[bi] || {};
      var bl = $('.dl-band', el); if (bl && band.label) bl.textContent = band.label;
      var wrap = el.parentNode, bt = wrap && wrap.querySelector('.bt');
      if (bt) $$('.bt-c', bt).forEach(function (c, i) { c.classList.toggle('on', i === bi); });
    });
  }

  /* ---------- context branch: pick the regimen, never hide it ---------- */
  var SITE_RX = { ucsf: /ucsf|parnassus|mission bay|mt\.? zion|mount zion|moffitt/i, zsfg: /zsfg|zuckerberg|sfgh|general hospital/i, va: /\bva\b|vasf|sfva|veteran/i, bch: /\bbch\b|benioff|children|oakland/i };
  /* The chosen context is part of the address. Every regimen panel already carries a
     unique id and the command palette already deep-links to it; the click handler just
     never wrote it back. replaceState, not pushState: flipping between five tabs should
     not bury the previous page under five history entries. */
  /* A heading standing over an empty box is read as an answer: "Alternative" followed by
     whitespace says there is no alternative. Sections whose panels are all filtered out
     for the active context are hidden outright. The build emits an explicit "IDMP lists
     no alternative regimen for X" panel wherever it can; this catches the rest. */
  function hideEmptySections() {
    $$('section.money, section.alt-blk, section.detail, section.cv, section.block').forEach(function (sec) {
      var kids = $$('[data-ctx]', sec);
      if (!kids.length) return;
      sec.hidden = !kids.some(function (k) { return !k.classList.contains('dim'); });
    });
  }

  function ctxWriteHash(id) {
    if (!id || location.hash === '#' + id) return;
    try { history.replaceState(null, '', '#' + id); } catch (e) {}
  }
  function ctxApply(nav, id, scroll) {
    // a page can carry more than one context block; only touch the panels this nav owns
    var ids = $$('.ctx-n', nav).map(function (b) { return b.getAttribute('data-ctx'); });
    var owned = function (el) { return ids.indexOf(el.getAttribute('data-ctx')) >= 0; };
    var many = ids.length > 4;
    var group = $('.ctx-ns', nav), listRole = group && group.getAttribute('role');
    var selAttr = listRole === 'radiogroup' ? 'aria-checked' : 'aria-selected';
    $$('.ctx-n', nav).forEach(function (b) {
      var on = b.getAttribute('data-ctx') === id;
      b.classList.toggle('on', on);
      if (b.getAttribute('role')) { b.setAttribute(selAttr, on ? 'true' : 'false'); b.tabIndex = on ? 0 : -1; }
    });
    $$('.rgc[data-ctx]').filter(owned).forEach(function (c) {
      var mine = c.getAttribute('data-ctx') === id;
      c.classList.toggle('dim', many && !mine);
      c.classList.toggle('lit', !many && mine);
    });
    $$('.dtl[data-ctx]').filter(owned).forEach(function (d) { d.classList.toggle('dim', many && d.getAttribute('data-ctx') !== id); });
    // a one-at-a-time block has a single Copy in its band; point it at the panel on show
    $$('.money').forEach(function (m) {
      if (!m.querySelector('.rgc[data-ctx="' + id + '"]')) return;
      var b = m.querySelector('.money-hd .copy'); if (b) b.setAttribute('data-copy', id);
    });
    $$('.rgx').forEach(function (g) {
      var vis = $$('.rgc:not(.dim)', g).length;
      g.setAttribute('data-cols', String(Math.min(vis || 1, 4)));
    });
    hideEmptySections();
    if (scroll) { var t = document.getElementById(id); if (t) t.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
  }
  function ctxAuto() {
    $$('.ctx').forEach(function (nav) {
      if (nav.getAttribute('data-user')) return;
      var btns = $$('.ctx-n', nav), pick = null;
      var hash = location.hash.replace('#', '');
      if (hash && btns.some(function (b) { return b.getAttribute('data-ctx') === hash; })) pick = hash;
      if (!pick) {
        var anySite = lens.where && btns.some(function (b) { return Object.keys(SITE_RX).some(function (k) { return SITE_RX[k].test(b.textContent); }); });
        var best = null, bestScore = 0;
        btns.forEach(function (b) {
          var score = 0, tags = (b.getAttribute('data-tags') || '').split(/\s+/);
          if (anySite && SITE_RX[lens.where] && SITE_RX[lens.where].test(b.textContent)) score += 3;
          else if (anySite && lens.where) score -= 1;
          if (lens.setting && tags.indexOf(lens.setting) >= 0) score += 2;
          if (score > bestScore) { bestScore = score; best = b; }
        });
        if (best) pick = best.getAttribute('data-ctx');
      }
      // the opening context is the first in curated order (curation.json settings.<slug>.order)
      if (!pick && btns.length) pick = btns[0].getAttribute('data-ctx');
      if (pick) { ctxApply(nav, pick, false); ctxAnnounce(nav, pick); }
    });
  }
  /* Selecting a context silently replaces the whole treatment section. The class on the
     button was the only trace of it; a screen reader user heard nothing at all. The role
     and aria-selected say which control is active, and this says what happened to the page. */
  var ctxLive = $('#ctx-live'), ctxReady = false;
  function ctxAnnounce(nav, id) {
    if (!ctxLive || !ctxReady) return;
    var b = $('.ctx-n[data-ctx="' + id + '"]', nav);
    if (b) ctxLive.textContent = 'Showing ' + b.textContent.trim() + '.';
  }
  function ctxPick(b, scroll) {
    var nav = b.closest('.ctx'); nav.setAttribute('data-user', '1');
    var id = b.getAttribute('data-ctx');
    ctxApply(nav, id, scroll);
    ctxWriteHash(id);
    ctxAnnounce(nav, id);
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('.ctx-n'); if (!b) return;
    ctxPick(b, true);
  });
  // roving tabindex: arrows move between contexts and select as they go, Home/End jump
  document.addEventListener('keydown', function (ev) {
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
    var b = ev.target.closest && ev.target.closest('.ctx-n'); if (!b) return;
    var list = $$('.ctx-n', b.closest('.ctx-ns')), i = list.indexOf(b), n = list.length, j = -1;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') j = (i + 1) % n;
    else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') j = (i - 1 + n) % n;
    else if (ev.key === 'Home') j = 0;
    else if (ev.key === 'End') j = n - 1;
    else return;
    ev.preventDefault();
    list[j].focus();
    ctxPick(list[j], false);
  });
  window.addEventListener('hashchange', function () { $$('.ctx').forEach(function (n) { n.removeAttribute('data-user'); }); ctxAuto(); });
  document.addEventListener('click', function (ev) { var li = ev.target.closest('ul.check > li'); if (li && !ev.target.closest('a')) li.classList.toggle('done'); });

  /* ---------- copy the shown regimen as plain text for a note ---------- */
  function serializeRegimen(col) {
    if (!col) return '';
    var body = $('.rgc-b', col); if (!body) return '';
    var parts = [];
    Array.prototype.forEach.call(body.children, function (node) {
      if (node.classList.contains('jn')) { parts.push(node.textContent.trim() ? 'with or without' : 'PLUS'); return; }
      if (node.classList.contains('rgc-lead') || node.classList.contains('rgc-tail')) { parts.push(node.textContent.trim()); return; }
      var drugs = [];
      $$('.rgd', node).length ? null : null;
      var scope = node.classList.contains('rgd') ? [node] : $$('.rgd', node);
      scope.forEach(function (d) {
        var name = ($('.rgd-n', d) || {}).textContent || '';
        var dose = ($('.dl-d', d) || {}).textContent || '';
        var note = ($('.rgd-note', d) || {}).textContent || '';
        var gapEl = $('.gap', d);
        var line = name.trim() + (dose ? ' ' + dose.trim() : (gapEl ? ' [no dose published on IDMP]' : ''));
        if (note) line += ' (' + note.trim() + ')';
        drugs.push(line);
      });
      if (drugs.length) parts.push(drugs.join(node.classList.contains('stp-any') ? ' OR ' : ' '));
    });
    return parts.filter(Boolean).join(' ');
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.copy[data-copy]'); if (!b) return;
    var id = b.getAttribute('data-copy');
    var first = $('.money .rgc[data-ctx="' + id + '"]');
    var alt = $('.alt-blk .rgc[data-ctx="' + id + '"]');
    var head = first && $('.rgc-h', first);
    var ctxName = head ? head.textContent.replace(/\s*Copy\s*$/, '').trim() : '';
    var title = ($('h1') || {}).textContent || '';
    var dur = first && $('.rgc-d b', first);
    var altHead = alt && $('.rgc-h', alt);
    var altCond = altHead ? altHead.textContent.replace(/\s*Copy\s*$/, '').replace(ctxName, '').replace(/^[\s—-]+/, '').trim() : '';
    var out = title.trim() + (ctxName ? ', ' + ctxName : '') + '\n';
    var f = serializeRegimen(first);
    if (f) out += 'First choice: ' + f + '\n';
    var a2 = serializeRegimen(alt);
    if (a2) out += 'Alternative' + (altCond ? ' (' + altCond + ')' : '') + ': ' + a2 + '\n';
    if (dur) out += 'Duration: ' + dur.textContent.trim() + '\n';
    var rev = $('.prov time');
    out += 'Per UCSF IDMP' + (rev ? ', revised ' + rev.textContent.trim() : '') + ': ' + location.href.split('#')[0];
    (navigator.clipboard ? navigator.clipboard.writeText(out) : Promise.reject())
      .then(function () { toast('Regimen copied'); }, function () { window.prompt('Copy:', out); });
  });

  /* ---------- recents (feed the empty search box; no UI of their own) ---------- */
  var recents = read('recents', true) || [];
  (function noteRecent() {
    var art = $('main article.node'); if (!art) return;
    var route = location.pathname.split('/').slice(-2).join('/').replace(/^\//, '');
    var entry = { u: route, n: ($('h1') || {}).textContent || '', t: html.getAttribute('data-section') };
    recents = [entry].concat(recents.filter(function (r) { return r.u !== entry.u; })).slice(0, 12); store('recents', recents);
  })();

  /* ---------- modals ---------- */
  function openModal(id) { var m = document.getElementById(id); if (!m) return; $$('.ovl').forEach(function (x) { x.hidden = true; }); m.hidden = false; document.body.style.overflow = 'hidden'; var inp = $('input', m); if (id === 'palette') { ensureIndex().then(renderPalette); } if (inp && id === 'palette') { inp.focus(); inp.select(); } }
  function closeModals() { $$('.ovl').forEach(function (m) { m.hidden = true; }); if (drawer && drawer.hidden !== false) document.body.style.overflow = ''; }
  document.addEventListener('click', function (ev) {
    var o = ev.target.closest('[data-open]'); if (o) { ev.preventDefault(); openModal(o.getAttribute('data-open')); return; }
    if (ev.target.closest('[data-close]')) { closeModals(); return; }
    if (ev.target.classList && ev.target.classList.contains('ovl')) closeModals();
  });
  var askOpen = $('#ask-open'); if (askOpen) askOpen.addEventListener('click', function () { openModal('palette'); });
  var lensOpen = $('#lens-open'); if (lensOpen) lensOpen.addEventListener('click', function () { openModal('lens'); });
  document.addEventListener('keydown', function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); var p = $('#palette'); if (p && !p.hidden) closeModals(); else openModal('palette'); return; }
    if (ev.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) { ev.preventDefault(); openModal('palette'); return; }
    if (ev.key === 'Escape') { if ($$('.ovl:not([hidden])').length) closeModals(); else if (drawer && !drawer.hidden) closeDrawer(true); else if (html.getAttribute('data-idxopen') === '1') setIdxOpen(false); }
  });

  /* ---------- palette: the Ask bar ---------- */
  var q = $('#q'), results = $('#results'), index = null, loading = null, sel = -1;
  var TYPES = { dx: 'Adult Rx', dxp: 'Peds Rx', drug: 'Dosing', gl: 'Guideline', abx: 'Antibiogram', bug: 'Organism', page: 'Page', person: 'Person', pub: 'Paper' };
  function ensureIndex() { if (index) return Promise.resolve(); if (!loading) loading = fetchJSON(root + 'index.json').then(function (j) { index = j; }).catch(function () { index = []; }); return loading; }
  var SETTING_KW = { icu: 'icu', intensive: 'icu', ward: 'inpatient', floor: 'inpatient', admitted: 'inpatient', inpatient: 'inpatient', hospitalized: 'inpatient', outpatient: 'outpatient', clinic: 'outpatient', ambulatory: 'outpatient', 'op': 'outpatient' };
  var SITE_KW = { zsfg: 'zsfg', sfgh: 'zsfg', general: 'zsfg', va: 'va', vasf: 'va', sfva: 'va', veterans: 'va', ucsf: 'ucsf', parnassus: 'ucsf', moffitt: 'ucsf', mission: 'ucsf', bch: 'bch', oakland: 'bch', benioff: 'bch' };
  var ALLERGY_KW = ['pcn', 'penicillin', 'allergy', 'allergic', 'beta-lactam', 'betalactam', 'bl', 'anaphylaxis'];
  var RENAL_KW = { hd: 'hd', hemodialysis: 'hd', dialysis: 'hd', ihd: 'hd', crrt: 'crrt', cvvh: 'crrt', cvvhd: 'crrt', cvvhdf: 'crrt' };
  var PEDS_KW = ['peds', 'pediatric', 'pediatrics', 'child', 'children', 'kid', 'infant', 'neonatal', 'neonate'];
  function parseQuery(raw) {
    var s = norm(raw), out = { setting: '', site: '', allergy: false, renal: '', crcl: null, peds: false, terms: [] };
    var m = s.match(/(?:crcl|gfr|egfr|clearance|cr\s*cl)\s*(?:of|=|:|is)?\s*(\d{1,3})/); if (m) { out.crcl = +m[1]; s = s.replace(m[0], ' '); }
    s.split(' ').forEach(function (t) {
      if (!t) return;
      if (SETTING_KW[t]) { out.setting = SETTING_KW[t]; return; }
      if (SITE_KW[t]) { out.site = SITE_KW[t]; return; }
      if (ALLERGY_KW.indexOf(t) >= 0) { out.allergy = true; return; }
      if (RENAL_KW[t]) { out.renal = RENAL_KW[t]; return; }
      if (PEDS_KW.indexOf(t) >= 0) { out.peds = true; return; }
      if (/^\d{1,3}$/.test(t) && out.crcl === null) { out.crcl = +t; return; }
      out.terms.push(t);
    });
    return out;
  }
  function matchDrug(e, terms) { var n = norm(e.n), k = ' ' + norm(e.k) + ' '; var used = 0; terms.forEach(function (t) { if (n.indexOf(t) >= 0 || k.indexOf(' ' + t) >= 0) used++; }); return used; }
  function wordHit(words, t) { return words.some(function (w) { return w.indexOf(t) === 0 || (t.length <= 2 && t.replace('.', '') && w[0] === t[0]); }); }
  function matchBug(e, terms) { var words = norm(e.n).split(' ').filter(Boolean), out = []; terms.forEach(function (t) { if (wordHit(words, t)) out.push(t); }); return out; }
  function matchCols(e, terms) {
    var hits = [];
    Object.keys(e.d || {}).forEach(function (abbr) {
      var name = norm(e.d[abbr][0] || ''), ab = norm(abbr), parts = name.split('/');
      terms.forEach(function (t) { if (ab === t || ab.indexOf(t) === 0 || name.indexOf(t) === 0 || parts.some(function (pp) { return pp.indexOf(t) === 0; })) { if (hits.indexOf(abbr) < 0) hits.push(abbr); } });
    });
    return hits;
  }
  function colTerms(e, terms) { var out = []; terms.forEach(function (t) { if (matchCols(e, [t]).length) out.push(t); }); return out; }
  function scoreEntry(e, terms, phrase) {
    var n = norm(e.n), k = norm(e.k || ''), s = norm(e.s || ''), sc = 0;
    if (!terms.length) return 0;
    if (n === phrase) sc += 120; else if (n.indexOf(phrase) === 0) sc += 90; else if (n.indexOf(phrase) >= 0) sc += 70;
    if (phrase.length > 2 && (' ' + k + ' ').indexOf(' ' + phrase + ' ') >= 0) sc += 66;
    for (var i = 0; i < terms.length; i++) {
      var t = terms[i];
      if ((' ' + n + ' ').indexOf(' ' + t) >= 0) sc += 28; else if (n.indexOf(t) >= 0) sc += 14;
      else if ((' ' + k + ' ').indexOf(' ' + t) >= 0) sc += 22; else if (k.indexOf(t) >= 0) sc += 8;
      else if (s.indexOf(t) >= 0) sc += 5; else return 0;
    }
    if (e.t === 'dx' || e.t === 'drug') sc += 4;
    if (lens.patient === 'peds' ? e.t === 'dxp' : e.t === 'dx') sc += 6;
    if (e.t === 'gl' && lens.where && e.sites && e.sites.indexOf(lens.where) >= 0) sc += 10;
    return sc;
  }
  function heatClass(v) { return v == null ? '' : v >= 90 ? 's-hi' : v >= 80 ? 's-ok' : v >= 60 ? 's-mid' : 's-lo'; }
  function bandLabel(dd, bi) { return dd.bands[bi] ? dd.bands[bi].label : ''; }
  function doseAnswer(e, pq) {
    var dd, bi = 0, label, anchor;
    if (pq.renal && e.hd) {
      dd = e.hd;
      bi = 0; for (var i = 0; i < dd.bands.length; i++) { if (dd.bands[i].kind === pq.renal) { bi = i; break; } }
      label = (pq.renal === 'hd' ? 'Intermittent HD' : 'CRRT') + ' → ' + (dd.bands[bi] ? dd.bands[bi].label : '');
      anchor = '#dialysis';
    } else if (pq.crcl !== null && e.dose) {
      dd = e.dose;
      var saved = lens.crcl; lens.crcl = String(pq.crcl); bi = pickBand(dd.bands); lens.crcl = saved;
      label = 'CrCl ' + pq.crcl + ' → ' + bandLabel(dd, bi);
      anchor = '#dosing';
    } else return '';
    var rows = dd.rows.slice(0, 5).map(function (r) { return '<span>' + esc(r.i || 'Dose') + '</span><span>' + esc(r.d[bi] || '—') + '</span>'; }).join('');
    return '<div class="answer"><div class="ah"><b>' + esc(e.n) + '</b><span class="muted">' + esc(label) + '</span></div><div class="ad">' + rows + '</div>' +
      '<div class="al"><a href="' + root + esc(e.u) + anchor + '">Open the full dosing table →</a> <span class="muted">From the IDMP dosing table for this drug.</span></div></div>';
  }
  function renderPalette() {
    if (!results) return;
    var raw = q.value, pq = parseQuery(raw), terms = pq.terms, phrase = terms.join(' ');
    sel = -1;
    if (!raw.trim()) {
      var chips = recents.slice(0, 6).map(function (r) { return '<a class="res" href="' + root + esc(r.u) + '"><span class="rt">Recent</span><span class="rn">' + esc(r.n) + '</span></a>'; }).join('');
      results.innerHTML = '<div class="empty">Type a syndrome, a drug with a CrCl, an organism with a drug, or a guideline. Modifiers understood: <b>icu</b>, <b>ward</b>, <b>outpatient</b>, <b>zsfg</b>, <b>va</b>, <b>peds</b>, <b>hd</b>, <b>crrt</b>, <b>pcn allergy</b>, <b>crcl 30</b>.<div class="hints">' +
        ['cap icu', 'cefepime crcl 30', 'e coli cipro', 'hap zsfg', 'vanc hd', 'uti outpatient', 'febrile neutropenia', 'zosyn'].map(function (h) { return '<button type="button" data-hint="' + h + '">' + h + '</button>'; }).join('') + '</div></div>' + (chips ? '<div class="res-group">Recent</div>' + chips : '');
      return;
    }
    var answers = [];
    // 1. drug + renal context => dose answer
    var drugHits = terms.length ? index.filter(function (e) { return e.t === 'drug' && matchDrug(e, terms) === terms.length; }) : [];
    if (drugHits.length && (pq.crcl !== null || pq.renal)) {
      drugHits.slice(0, 2).forEach(function (e) { var a = doseAnswer(e, pq); if (a) answers.push(a); });
    }
    // 2. organism (+ drug) => susceptibility answer. Terms split between organism words and drug columns.
    if (terms.length) {
      var seen = {}, bugAnswers = [];
      index.forEach(function (e) {
        if (e.t !== 'bug' || bugAnswers.length >= 4) return;
        var bt = matchBug(e, terms); if (!bt.length) return;
        var rest = terms.filter(function (t) { return bt.indexOf(t) < 0; });
        var cols = rest.length ? matchCols(e, rest) : [];
        var covered = bt.length + colTerms(e, rest).length;
        if (covered < terms.length) return;
        var key = e.n + '|' + e.tbl; if (seen[key]) return; seen[key] = 1;
        var show = cols.length ? cols : Object.keys(e.v);
        var cells = show.map(function (abbr) {
          var v = e.v[abbr], name = e.d[abbr] ? e.d[abbr][0] : abbr;
          return '<span class="' + heatClass(v) + '" title="' + esc(name) + '"><span>' + esc(abbr) + '</span><b>' + (v == null ? '—' : v + '%') + '</b></span>';
        }).join('');
        if (!cells) return;
        bugAnswers.push('<div class="answer bug"><div class="ah"><b><i>' + esc(e.n) + '</i></b><span class="muted">' + esc(e.s) + (e.n_iso ? ' · n=' + e.n_iso : '') + '</span></div><div class="ad">' + cells + '</div>' +
          '<div class="al"><a href="' + root + esc(e.u) + '">Open in the explorer →</a> <span class="muted">% susceptible as published.</span></div></div>');
      });
      answers = answers.concat(bugAnswers);
    }
    // 3. ranked entries, grouped; groups ordered by their best hit
    var hits = [];
    index.forEach(function (e) {
      if (e.t === 'bug') return;
      var sc = scoreEntry(e, terms, phrase); if (sc <= 0) return;
      if (pq.peds) { if (e.t === 'dx') sc -= 30; if (e.t === 'dxp') sc += 30; }
      if (pq.site && e.t === 'gl' && e.sites && e.sites.indexOf(pq.site) >= 0) sc += 40;
      if (pq.setting && (e.t === 'dx' || e.t === 'dxp') && e.rows && e.rows.some(function (r) { return (r.g || []).indexOf(pq.setting) >= 0; })) sc += 12;
      hits.push([sc, e]);
    });
    hits.sort(function (a, b) { return b[0] - a[0] || a[1].n.localeCompare(b[1].n); });
    var groups = { dx: { label: 'Empiric therapy', best: 0, rows: [] }, drug: { label: 'Dosing', best: 0, rows: [] }, gl: { label: 'Guidelines', best: 0, rows: [] }, other: { label: 'More', best: 0, rows: [] } };
    hits.slice(0, 18).forEach(function (h) {
      var sc = h[0], e = h[1], extra = '', href = root + e.u;
      if ((e.t === 'dx' || e.t === 'dxp') && e.rows && e.rows.length > 1) {
        var want = pq.setting || lens.setting, row = null;
        if (want) row = e.rows.filter(function (r) { return (r.g || []).indexOf(want) >= 0; })[0];
        if (!row) { var rt = terms.filter(function (t) { return norm(e.n).indexOf(t) < 0; }); if (rt.length) row = e.rows.filter(function (r) { return rt.every(function (t) { return norm(r.l).indexOf(t) >= 0; }); })[0]; }
        if (row) { href += '#' + row.a; extra = ' <span class="arrow">→</span> ' + esc(row.l); }
        else extra = '<span class="rs">' + esc(e.rows.length + ' situations: ' + e.rows.slice(0, 3).map(function (r) { return r.l; }).join(' · ') + (e.rows.length > 3 ? ' …' : '')) + '</span>';
      }
      if (pq.allergy && (e.t === 'dx' || e.t === 'dxp')) extra += ' <span class="badge">allergy column first</span>';
      var line = '<a class="res" href="' + href + '"><span class="rt">' + (TYPES[e.t] || e.t) + '</span><span class="rn">' + esc(e.n) + extra + (extra.indexOf('class="rs"') < 0 && e.s ? '<span class="rs">' + esc(e.s) + '</span>' : '') + '</span></a>';
      var g = (e.t === 'dx' || e.t === 'dxp') ? groups.dx : e.t === 'drug' ? groups.drug : e.t === 'gl' ? groups.gl : groups.other;
      g.rows.push(line); g.best = Math.max(g.best, sc);
    });
    var order = Object.keys(groups).filter(function (k) { return groups[k].rows.length; }).sort(function (a, b) { return groups[b].best - groups[a].best; });
    var out = answers.join('');
    order.forEach(function (k) { out += '<div class="res-group">' + groups[k].label + '</div>' + groups[k].rows.join(''); });
    results.innerHTML = out || '<div class="empty">No matches for “' + esc(raw) + '”. Try a brand name, an abbreviation, or an organism.</div>';
  }

  if (q && results) {
    q.addEventListener('input', function () { ensureIndex().then(renderPalette); });
    results.addEventListener('click', function (ev) { var h = ev.target.closest('button[data-hint]'); if (h) { q.value = h.getAttribute('data-hint'); renderPalette(); q.focus(); } });
    q.addEventListener('keydown', function (ev) {
      var items = $$('a.res', results);
      if (ev.key === 'ArrowDown') { ev.preventDefault(); sel = Math.min(sel + 1, items.length - 1); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); sel = Math.max(sel - 1, 0); }
      else if (ev.key === 'Enter') { ev.preventDefault(); var a = items[sel >= 0 ? sel : 0]; if (a) location.href = a.href; return; }
      else return;
      items.forEach(function (a, i) { a.classList.toggle('sel', i === sel); }); if (items[sel]) items[sel].scrollIntoView({ block: 'nearest' });
    });
    if (params.get('q')) { q.value = params.get('q'); openModal('palette'); }
  }

  /* ---------- index page filters ---------- */
  var filter = $('#filter'), active = {};
  function applyFilter() {
    var needle = norm(filter ? filter.value : ''), terms = needle.split(' ').filter(Boolean);
    $$('#list > tbody > tr, #list > li, #groups li').forEach(function (li) {
      var hay = norm(li.textContent + ' ' + (li.getAttribute('data-syn') || ''));
      var ok = terms.every(function (t) { return hay.indexOf(t) >= 0; });
      Object.keys(active).forEach(function (chip) {
        if (!active[chip]) return;
        if (chip.indexOf('site:') === 0) { if ((li.getAttribute('data-sites') || '').split(/\s+/).indexOf(chip.slice(5)) < 0) ok = false; }
        else if ((li.getAttribute('data-tags') || '').split(/\s+/).indexOf(chip) < 0) ok = false;
      });
      li.classList.toggle('hide', !ok);
    });
    $$('#groups .group').forEach(function (g) { g.classList.toggle('hide', !$$('li:not(.hide)', g).length); });
  }
  if (filter) {
    filter.addEventListener('input', applyFilter);
    $$('#chips button').forEach(function (b) { b.addEventListener('click', function () { var c = b.getAttribute('data-chip'); active[c] = !active[c]; b.classList.toggle('on', !!active[c]); applyFilter(); }); });
    var tag = params.get('tag'), site = params.get('site'), qq = params.get('filter');
    if (tag) { var b1 = $('#chips button[data-chip="' + tag + '"]'); if (b1) b1.click(); }
    if (site) { var b2 = $('#chips button[data-chip="site:' + groupOf(site) + '"]'); if (b2) b2.click(); }
    if (qq) { filter.value = qq; applyFilter(); }
    if (!tag && !site && lens.setting && $('#chips button[data-chip="' + lens.setting + '"]')) { $('#chips button[data-chip="' + lens.setting + '"]').click(); }
  }

  /* ---------- drug drawer ---------- */
  var drawer = $('#drawer'), dbody = $('#drawer-body'), scrim = $('#scrim'), openLink = $('#drawer-open'), backBtn = $('#drawer-back'), stack = [];
  function closeDrawer(pop) { if (!drawer || drawer.hidden) return; drawer.hidden = true; scrim.hidden = true; stack = []; document.body.style.overflow = ''; if (pop && history.state && history.state.drawer) history.back(); }
  function showDrawer(url, push) {
    fetch(url).then(function (r) { return r.text(); }).then(function (t) {
      var doc = new DOMParser().parseFromString(t, 'text/html');
      var art = doc.querySelector('.doc-in article.node') || doc.querySelector('.doc-in') || doc.querySelector('main');
      var head = doc.querySelector('.doc-head');
      dbody.innerHTML = '';
      if (head) {
        $$('.crumbs, .pin', head).forEach(function (x) { x.remove(); });
        dbody.appendChild(head);
      }
      dbody.appendChild(art);
      $$('a[href]', dbody).forEach(function (a) { a.setAttribute('href', new URL(a.getAttribute('href'), url).href); });
      $$('img[src]', dbody).forEach(function (i) { i.setAttribute('src', new URL(i.getAttribute('src'), url).href); });
      $$('.renal-dial, .jump', dbody).forEach(function (d) { d.remove(); });
      applyBands(dbody); dbody.scrollTop = 0; openLink.href = url; drawer.hidden = false; scrim.hidden = false; document.body.style.overflow = 'hidden';
      backBtn.hidden = stack.length < 2;
      if (push) history.pushState({ drawer: url }, '', location.href);
    }).catch(function () { location.href = url; });
  }
  if (drawer) {
    document.addEventListener('click', function (ev) {
      var a = ev.target.closest('a[data-drug]'); if (!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault(); var url = new URL(a.getAttribute('href'), location.href).href; stack.push(url); showDrawer(url, stack.length === 1);
    });
    $('#drawer-close').addEventListener('click', function () { closeDrawer(true); });
    scrim.addEventListener('click', function () { if (html.getAttribute('data-idxopen') === '1') setIdxOpen(false); closeDrawer(true); });
    backBtn.addEventListener('click', function () { stack.pop(); if (stack.length) showDrawer(stack[stack.length - 1], false); else closeDrawer(true); });
    window.addEventListener('popstate', function () { if (!drawer.hidden) closeDrawer(false); });
    document.addEventListener('lens-change', function () { if (!drawer.hidden) applyBands(dbody); });
  }

  /* ---------- outline highlight ---------- */
  var outlineLinks = $$('.outline a');
  if (outlineLinks.length && 'IntersectionObserver' in window) {
    var byId = {}; outlineLinks.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
    var io = new IntersectionObserver(function (entries) { entries.forEach(function (en) { if (en.isIntersecting) { outlineLinks.forEach(function (a) { a.classList.remove('on'); }); var a = byId[en.target.id]; if (a) a.classList.add('on'); } }); }, { rootMargin: '-100px 0px -70% 0px' });
    Object.keys(byId).forEach(function (id) { var el = document.getElementById(id); if (el) io.observe(el); });
  }

  /* ---------- sync status pill ---------- */
  var pill = $('#status');
  function ago(iso) { var h = Math.round((Date.now() - new Date(iso).getTime()) / 36e5); if (h < 1) return 'just now'; if (h < 36) return h + 'h ago'; return Math.round(h / 24) + 'd ago'; }
  if (pill) {
    fetchJSON(root + 'status.json').then(function (st) {
      var when = st.run || st.built, days = (Date.now() - new Date(when).getTime()) / 864e5, cls = st.ok ? 'ok' : 'bad';
      if (st.ok && days > 3) cls = 'warn'; if (days > 10) cls = 'bad';
      pill.className = 'sync ' + cls; pill.textContent = (st.ok ? 'synced ' : 'sync issues · ') + ago(when);
      pill.title = 'Last verified against idmp.ucsf.edu ' + new Date(when).toLocaleString() + (st.problems && st.problems.length ? ' · ' + st.problems.length + ' note(s)' : '');
      return fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/sync.yml/runs?per_page=1&status=completed').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j || !j.workflow_runs || !j.workflow_runs.length) return;
        var run = j.workflow_runs[0];
        if (run.conclusion !== 'success' && new Date(run.updated_at) > new Date(when)) { pill.className = 'status bad'; pill.textContent = 'sync failing · ' + ago(run.updated_at); pill.href = run.html_url; }
      });
    }).catch(function () {});
  }

  /* ---------- bug-drug explorer ---------- */
  var explore = $('#explore');
  if (explore) {
    var out = $('#explore-out'), bugIn = $('#bug'), drugIn = $('#abx-drug'), tables = [];
    fetchJSON(root + 'antibiogram.json').then(function (j) {
      tables = j.tables || [];
      var bugs = {}, drugs = {};
      tables.forEach(function (t) { t.rows.forEach(function (r) { bugs[r.organism] = 1; }); t.drugs.forEach(function (d) { drugs[d.name || d.abbr] = 1; }); });
      $('#bug-list').innerHTML = Object.keys(bugs).sort().map(function (b) { return '<option value="' + esc(b) + '">'; }).join('');
      $('#drug-list').innerHTML = Object.keys(drugs).sort().map(function (d) { return '<option value="' + esc(d) + '">'; }).join('');
      var pb = params.get('bug'), pd = params.get('drug');
      if (pb) { bugIn.value = pb; renderBug(pb); } else if (pd) { drugIn.value = pd; renderDrug(pd); } else renderOverview();
    }).catch(function () { out.innerHTML = '<p class="muted">Could not load antibiogram data.</p>'; });
    function cell(v, raw) { var c = heatClass(v); return '<td class="' + (v == null && /^r$/i.test(raw || '') ? 's-r' : c) + '">' + esc(raw || '—') + '</td>'; }
    function renderOverview() {
      out.innerHTML = tables.map(function (t) { return '<h2>' + esc(t.title) + ' <span class="muted">' + esc(t.year || '') + '</span></h2><div class="tbl-wrap"><table class="tbl heat"><tr><th>Organism</th><th>n</th>' + t.drugs.map(function (d) { return '<th title="' + esc(d.name) + '">' + esc(d.abbr) + '</th>'; }).join('') + '</tr>' + t.rows.map(function (r) { return '<tr><td>' + esc(r.organism) + '</td><td>' + (r.n == null ? '' : r.n) + '</td>' + t.drugs.map(function (d) { var v = r.v[d.abbr] || {}; return cell(v.v, v.raw); }).join('') + '</tr>'; }).join('') + '</table></div><p class="muted">Source: <a href="' + root + esc(t.route) + '">' + esc(t.page) + '</a></p>'; }).join('');
    }
    function renderBug(name) {
      var n = norm(name), blocks = [];
      tables.forEach(function (t) { t.rows.forEach(function (r) { if (norm(r.organism).indexOf(n) < 0 && n.indexOf(norm(r.organism)) < 0) return;
        blocks.push('<h2><i>' + esc(r.organism) + '</i> <span class="muted">' + esc(t.title) + ' ' + esc(t.year || '') + (r.n != null ? ' · n=' + r.n : '') + '</span></h2><div class="tbl-wrap"><table class="tbl heat"><tr><th>Drug</th><th>% susceptible</th></tr>' + t.drugs.map(function (d) { var v = r.v[d.abbr] || {}; return '<tr><td>' + (d.slug ? '<a data-drug="' + esc(d.slug) + '" href="' + root + 'drugs/' + esc(d.slug) + '.html">' + esc(d.name || d.abbr) + '</a>' : esc(d.name || d.abbr)) + ' <span class="muted">' + esc(d.abbr) + '</span></td>' + cell(v.v, v.raw) + '</tr>'; }).join('') + '</table></div>'); }); });
      out.innerHTML = blocks.join('') || '<p class="muted">No organism matches “' + esc(name) + '” in the parsed tables.</p>';
    }
    function renderDrug(name) {
      var n = norm(name), blocks = [];
      tables.forEach(function (t) { t.drugs.forEach(function (d) { if (norm(d.name || '').indexOf(n) < 0 && norm(d.abbr).indexOf(n) < 0) return;
        blocks.push('<h2>' + esc(d.name || d.abbr) + ' <span class="muted">' + esc(t.title) + ' ' + esc(t.year || '') + '</span></h2><div class="tbl-wrap"><table class="tbl heat"><tr><th>Organism</th><th>n</th><th>% susceptible</th></tr>' + t.rows.map(function (r) { var v = r.v[d.abbr] || {}; return '<tr><td><i>' + esc(r.organism) + '</i></td><td>' + (r.n == null ? '' : r.n) + '</td>' + cell(v.v, v.raw) + '</tr>'; }).join('') + '</table></div>'); }); });
      out.innerHTML = blocks.join('') || '<p class="muted">No drug matches “' + esc(name) + '” in the parsed tables.</p>';
    }
    bugIn.addEventListener('input', function () { if (bugIn.value.length > 1) { drugIn.value = ''; renderBug(bugIn.value); } else if (!bugIn.value) renderOverview(); });
    drugIn.addEventListener('input', function () { if (drugIn.value.length > 1) { bugIn.value = ''; renderDrug(drugIn.value); } else if (!drugIn.value) renderOverview(); });
  }

  /* ---------- index sidebar ---------- */
  var idxWrap = $('#idx-wrap'), idxQ = $('#idx-q'), navTree = null, navLoading = null;
  function ensureNav() {
    if (navTree) return Promise.resolve(navTree);
    if (!navLoading) navLoading = fetchJSON(root + 'nav.json').then(function (j) { navTree = j; return j; }).catch(function () { navTree = {}; return {}; });
    return navLoading;
  }
  function renderSection(sec, groups) {
    var body = $('.idx-body', sec);
    if (!body || body.getAttribute('data-filled') === '1') return;
    body.innerHTML = (groups || []).map(function (g) {
      return '<div class="idx-grp"' + (g.pop ? ' data-pop="' + esc(g.pop.toLowerCase()) + '"' : '') + '>' +
        (g.label ? '<div class="idx-grp-h">' + (g.pop ? '<span class="idx-pop">' + esc(g.pop) + '</span>' : '') + esc(g.label) + '</div>' : '') +
        '<ul>' + g.items.map(function (it) { return '<li><a href="' + root + esc(it.u) + '">' + esc(it.t) + '</a></li>'; }).join('') + '</ul></div>';
    }).join('');
    body.setAttribute('data-filled', '1');
    applyIdxPop();
  }
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest('.idx-toggle'); if (!t) return;
    var sec = t.closest('.idx-sec'), body = $('.idx-body', sec);
    if (!body) return;
    var open = t.getAttribute('aria-expanded') === 'true';
    if (open) { body.hidden = true; t.setAttribute('aria-expanded', 'false'); return; }
    ensureNav().then(function (tree) {
      renderSection(sec, tree[sec.getAttribute('data-sec')]);
      body.hidden = false; t.setAttribute('aria-expanded', 'true');
      if (idxQ && idxQ.value) filterIdx();
    });
  });
  function filterIdx() {
    var n = norm(idxQ.value), terms = n.split(' ').filter(Boolean);
    $$('.idx-sec').forEach(function (sec) {
      var any = false;
      $$('.idx-grp', sec).forEach(function (g) {
        if (g.hidden) { g.classList.add('hide'); return; }
        var gAny = false;
        $$('li', g).forEach(function (li) {
          var ok = !terms.length || terms.every(function (t) { return norm(li.textContent).indexOf(t) >= 0; });
          li.classList.toggle('hide', !ok); if (ok) gAny = true;
        });
        g.classList.toggle('hide', !gAny); if (gAny) any = true;
      });
      var head = norm(sec.textContent.slice(0, 40));
      sec.classList.toggle('hide', !!terms.length && !any && !terms.every(function (t) { return head.indexOf(t) >= 0; }));
    });
  }
  if (idxQ) {
    idxQ.addEventListener('input', function () {
      if (idxQ.value.length > 1) {
        ensureNav().then(function (tree) {
          $$('.idx-sec').forEach(function (sec) {
            var body = $('.idx-body', sec);
            if (body && body.getAttribute('data-filled') !== '1' && !$('.idx-grp', body)) renderSection(sec, tree[sec.getAttribute('data-sec')]);
            if (body) { body.hidden = false; var tg = $('.idx-toggle', sec); if (tg) tg.setAttribute('aria-expanded', 'true'); }
          });
          filterIdx();
        });
      } else { filterIdx(); }
    });
    idxQ.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { idxQ.value = ''; filterIdx(); idxQ.blur(); }
      if (ev.key === 'Enter') { var first = $('.idx-grp li:not(.hide) a'); if (first) location.href = first.href; }
    });
  }
  /* Below the drawer breakpoint the index is a closed off-canvas panel. transform alone
     only moves it off screen: all of its links stay in the tab order, ahead of the
     article, so a keyboard or switch-control user traverses an invisible index before
     reaching the page. inert removes it from the tab order and the accessibility tree
     together, and costs nothing above the breakpoint, where the drawer is a real sidebar. */
  var idxMQ = window.matchMedia('(max-width: 900px)');
  function syncIdxInert() {
    if (!idxWrap) return;
    var closed = idxMQ.matches && html.getAttribute('data-idxopen') !== '1';
    if (!closed) { idxWrap.removeAttribute('inert'); return; }
    // never strand the caret inside a panel that is about to stop existing
    if (idxWrap.contains(document.activeElement)) { var mb = $('#idx-open'); (mb || document.body).focus(); }
    idxWrap.setAttribute('inert', '');
  }
  (idxMQ.addEventListener ? idxMQ.addEventListener.bind(idxMQ, 'change') : idxMQ.addListener.bind(idxMQ))(syncIdxInert);
  function setIdxOpen(on) {
    if (on) html.setAttribute('data-idxopen', '1'); else html.removeAttribute('data-idxopen');
    var mb = $('#idx-open'); if (mb) mb.setAttribute('aria-expanded', on ? 'true' : 'false');
    if (scrim) scrim.hidden = !on || !drawer.hidden;
    document.body.style.overflow = on ? 'hidden' : '';
    syncIdxInert();
  }
  $$('#idx-open, #idx-open-2').forEach(function (b) {
    b.addEventListener('click', function () { setIdxOpen(html.getAttribute('data-idxopen') !== '1'); });
  });
  if (idxWrap) idxWrap.addEventListener('click', function (ev) { if (ev.target.closest('a') && window.innerWidth <= 900) setIdxOpen(false); });
  /* Keep the current item in view. This was computed once, from a deferred script, against
     a 4233px list, before Inter arrived from Google Fonts with display=swap: on a cold load
     the fallback metrics put the active row 1730px above the visible area, so the nav showed
     pediatric sections while you were on an adult page. Warm loads landed correctly, which
     is the worst possible failure mode. Recompute when the webfont lands and whenever the
     list resizes. offsetTop is measured against the offsetParent, so read clientHeight and
     set scrollTop on that same box rather than on two different ones. */
  function centerCurrent() {
    var scroller = $('.idx');
    var cur = $('.idx-grp a[aria-current="page"]');
    if (!cur || !scroller) return;
    var top = cur.offsetTop - (scroller.clientHeight / 2) + (cur.offsetHeight / 2);
    scroller.scrollTop = Math.max(0, Math.min(top, scroller.scrollHeight - scroller.clientHeight));
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(centerCurrent);
  if (window.ResizeObserver) {
    var idxRO = new ResizeObserver(function () { centerCurrent(); });
    var idxEl = $('.idx'); if (idxEl) idxRO.observe(idxEl);
  }

  /* ---------- offline: service worker + whole-site download ---------- */
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    navigator.serviceWorker.register(root + 'sw.js', { scope: root || './' }).catch(function () {});
    navigator.serviceWorker.addEventListener('message', function (ev) { var d = ev.data || {}; if (d.type === 'precache-progress') toast('Saving for offline… ' + d.done + '/' + d.total, 2500); if (d.type === 'precache-done') toast('Whole site saved for offline use', 3000); });
  }
  $$('#offline-btn, #offline-btn-2').forEach(function (b) { b.addEventListener('click', function () {
    if (!('serviceWorker' in navigator)) { toast('Offline saving is not supported in this browser'); return; }
    fetchJSON(root + 'pages.json').then(function (j) { return navigator.serviceWorker.ready.then(function (reg) { if (!reg.active) throw new Error(); reg.active.postMessage({ type: 'precache', pages: j.pages }); toast('Saving ' + j.pages.length + ' files for offline…', 2500); }); }).catch(function () { toast('Could not start the offline download'); });
  }); });

  applyLens(false);
  ctxReady = true;   // nothing to announce about the state the page loaded in
  syncIdxInert();
  centerCurrent();
  // ctxAuto has already honoured location.hash by now, so the panel is on screen and
  // scrollable; the browser could not reach it during parse while it was display:none.
  (function () {
    var h = location.hash.replace('#', '');
    if (!h || !/^rx-\d+$/.test(h)) return;
    var t = document.getElementById(h);
    if (t) requestAnimationFrame(function () { t.scrollIntoView({ block: 'nearest', behavior: 'auto' }); });
  })();
})();
