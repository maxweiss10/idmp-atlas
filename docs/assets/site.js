/* IDMP Atlas client: theme, site switch, search, filters, drug drawer, sync status. No dependencies. */
(function () {
  'use strict';
  var html = document.documentElement;
  var root = html.getAttribute('data-root') || '';
  var REPO = 'maxweiss10/idmp-atlas';
  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function store(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  function read(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  /* ---- theme ---- */
  var themeBtn = $('#theme');
  function paintTheme() {
    var t = html.getAttribute('data-theme');
    var dark = t === 'dark' || (!t && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (themeBtn) themeBtn.textContent = dark ? '☀' : '☾';
  }
  if (themeBtn) themeBtn.addEventListener('click', function () {
    var t = html.getAttribute('data-theme');
    var dark = t === 'dark' || (!t && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var next = dark ? 'light' : 'dark';
    html.setAttribute('data-theme', next); store('theme', next); paintTheme();
  });
  paintTheme();

  /* ---- site switch ---- */
  function applySite(site) {
    html.setAttribute('data-site', site || '');
    $$('.site-switch button').forEach(function (b) { b.classList.toggle('on', (b.getAttribute('data-site') || '') === (site || '')); });
    $$('li[data-sites]').forEach(function (li) {
      var sites = (li.getAttribute('data-sites') || '').split(/\s+/);
      li.classList.toggle('site-match', !site || sites.indexOf(site) >= 0);
    });
    // bring matching items to the top of any related list
    $$('ul.rel-list').forEach(function (ul) {
      var items = $$('li[data-sites]', ul);
      if (!items.length) return;
      items.sort(function (a, b) { return (b.classList.contains('site-match') ? 1 : 0) - (a.classList.contains('site-match') ? 1 : 0); });
      items.forEach(function (li) { ul.appendChild(li); });
    });
  }
  $$('.site-switch button').forEach(function (b) {
    b.addEventListener('click', function () { var s = b.getAttribute('data-site') || ''; store('site', s || null); applySite(s); });
  });
  var params = new URLSearchParams(location.search);
  applySite(params.get('site') ? groupOf(params.get('site')) : (read('site') || ''));
  function groupOf(slug) { slug = slug.toLowerCase(); if (/zuckerberg|zsfg/.test(slug)) return 'zsfg'; if (/veteran|^va/.test(slug)) return 'va'; if (/benioff|children/.test(slug)) return 'bch'; return 'ucsf'; }

  /* ---- sync status pill ---- */
  var pill = $('#status');
  function ago(iso) {
    var ms = Date.now() - new Date(iso).getTime(); var h = Math.round(ms / 36e5);
    if (h < 1) return 'just now'; if (h < 36) return h + 'h ago'; return Math.round(h / 24) + 'd ago';
  }
  if (pill) {
    fetch(root + 'status.json', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (st) {
      var when = st.run || st.built; var days = (Date.now() - new Date(when).getTime()) / 864e5;
      var cls = st.ok ? 'ok' : 'warn';
      if (days > 3) cls = 'warn'; if (days > 10) cls = 'bad';
      pill.className = 'status ' + cls;
      pill.textContent = (st.ok ? 'synced ' : 'sync issues · ') + ago(when);
      pill.title = 'Last verified against idmp.ucsf.edu ' + new Date(when).toLocaleString() + (st.problems && st.problems.length ? ' · ' + st.problems.length + ' problem(s)' : '');
      // best effort: has the scheduled job itself been failing since the last successful build?
      return fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/sync.yml/runs?per_page=1&status=completed').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j || !j.workflow_runs || !j.workflow_runs.length) return;
        var run = j.workflow_runs[0];
        if (run.conclusion !== 'success' && new Date(run.updated_at) > new Date(when)) {
          pill.className = 'status bad'; pill.textContent = 'sync failing · ' + ago(run.updated_at); pill.href = run.html_url; pill.title = 'The nightly sync job is failing; content may be stale.';
        }
      });
    }).catch(function () {});
  }

  /* ---- search ---- */
  var q = $('#q'), box = $('#results'), index = null, sel = -1;
  var TYPES = { dx: 'Adult Rx', dxp: 'Peds Rx', drug: 'Dosing', gl: 'Guideline', abx: 'Antibiogram', page: 'Page', person: 'Person', pub: 'Paper' };
  function norm(s) { return (s || '').toLowerCase().replace(/[^a-z0-9%\/\-\. ]+/g, ' ').replace(/\s+/g, ' ').trim(); }
  function score(e, terms, phrase) {
    var n = norm(e.n), k = norm(e.k), s = norm(e.s), sc = 0;
    if (n === phrase) sc += 120; else if (n.indexOf(phrase) === 0) sc += 90; else if (n.indexOf(phrase) >= 0) sc += 70;
    if (phrase.length > 2 && (' ' + k + ' ').indexOf(' ' + phrase + ' ') >= 0) sc += 60;
    for (var i = 0; i < terms.length; i++) {
      var t = terms[i];
      if ((' ' + n + ' ').indexOf(' ' + t) >= 0) sc += 28; else if (n.indexOf(t) >= 0) sc += 14;
      else if ((' ' + k + ' ').indexOf(' ' + t) >= 0) sc += 18; else if (k.indexOf(t) >= 0) sc += 8;
      else if (s.indexOf(t) >= 0) sc += 5; else return 0; // every term must hit somewhere
    }
    if (e.t === 'dx' || e.t === 'drug') sc += 3;
    return sc;
  }
  function render(list, phrase) {
    if (!list.length) { box.innerHTML = '<div class="empty">No matches for “' + phrase.replace(/</g, '&lt;') + '”. Try a brand name, an abbreviation, or an organism.</div>'; box.hidden = false; return; }
    box.innerHTML = list.map(function (e) {
      return '<a href="' + root + e.u + '"><span class="rt">' + (TYPES[e.t] || e.t) + '</span><span class="rn">' + e.n.replace(/</g, '&lt;') + (e.s ? '<span class="rs">' + e.s.replace(/</g, '&lt;') + '</span>' : '') + '</span></a>';
    }).join('');
    box.hidden = false; sel = -1;
  }
  function run() {
    var phrase = norm(q.value); if (!phrase) { box.hidden = true; return; }
    var terms = phrase.split(' ').filter(Boolean);
    var hits = [];
    for (var i = 0; i < index.length; i++) { var sc = score(index[i], terms, phrase); if (sc > 0) hits.push([sc, index[i]]); }
    hits.sort(function (a, b) { return b[0] - a[0] || a[1].n.localeCompare(b[1].n); });
    render(hits.slice(0, 14).map(function (h) { return h[1]; }), phrase);
  }
  if (q && box) {
    var loading = null;
    function ensure() { if (index) return Promise.resolve(); if (!loading) loading = fetch(root + 'search.json').then(function (r) { return r.json(); }).then(function (j) { index = j; }); return loading; }
    q.addEventListener('focus', ensure);
    q.addEventListener('input', function () { ensure().then(run); });
    q.addEventListener('keydown', function (ev) {
      var items = $$('a', box);
      if (ev.key === 'ArrowDown') { ev.preventDefault(); sel = Math.min(sel + 1, items.length - 1); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); sel = Math.max(sel - 1, 0); }
      else if (ev.key === 'Enter') { ev.preventDefault(); var a = items[sel >= 0 ? sel : 0]; if (a) location.href = a.href; return; }
      else if (ev.key === 'Escape') { box.hidden = true; q.blur(); return; }
      else return;
      items.forEach(function (a, i) { a.classList.toggle('sel', i === sel); }); if (items[sel]) items[sel].scrollIntoView({ block: 'nearest' });
    });
    document.addEventListener('click', function (ev) { if (!ev.target.closest('#search')) box.hidden = true; });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) { ev.preventDefault(); q.focus(); q.select(); }
    });
  }

  /* ---- index filters ---- */
  var filter = $('#filter');
  var active = {};
  function applyFilter() {
    var needle = norm(filter ? filter.value : '');
    var terms = needle.split(' ').filter(Boolean);
    var items = $$('#list > li, #groups li');
    items.forEach(function (li) {
      var hay = norm(li.textContent + ' ' + (li.getAttribute('data-syn') || ''));
      var ok = terms.every(function (t) { return hay.indexOf(t) >= 0; });
      Object.keys(active).forEach(function (chip) {
        if (!active[chip]) return;
        if (chip.indexOf('site:') === 0) { var s = (li.getAttribute('data-sites') || '').split(/\s+/); if (s.indexOf(chip.slice(5)) < 0) ok = false; }
        else { var tags = (li.getAttribute('data-tags') || '').split(/\s+/); if (tags.indexOf(chip) < 0) ok = false; }
      });
      li.classList.toggle('hide', !ok);
    });
    $$('#groups .group').forEach(function (g) { g.classList.toggle('hide', !$$('li:not(.hide)', g).length); });
  }
  if (filter) {
    filter.addEventListener('input', applyFilter);
    $$('#chips button').forEach(function (b) {
      b.addEventListener('click', function () { var c = b.getAttribute('data-chip'); active[c] = !active[c]; b.classList.toggle('on', !!active[c]); applyFilter(); });
    });
    var tag = params.get('tag'), site = params.get('site'), qq = params.get('q');
    if (tag) { var b = $('#chips button[data-chip="' + tag + '"]'); if (b) b.click(); }
    if (site) { var b2 = $('#chips button[data-chip="site:' + groupOf(site) + '"]'); if (b2) b2.click(); }
    if (qq) { filter.value = qq; applyFilter(); }
  }

  /* ---- drug drawer ---- */
  var drawer = $('#drawer'), body = $('#drawer-body'), scrim = $('#scrim'), openLink = $('#drawer-open'), backBtn = $('#drawer-back');
  var stack = [];
  function closeDrawer(pop) {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true; scrim.hidden = true; stack = []; document.body.style.overflow = '';
    if (pop && history.state && history.state.drawer) history.back();
  }
  function showDrawer(url, push) {
    fetch(url).then(function (r) { return r.text(); }).then(function (t) {
      var doc = new DOMParser().parseFromString(t, 'text/html');
      var art = doc.querySelector('main article.node') || doc.querySelector('main');
      $$('a[href]', art).forEach(function (a) { a.setAttribute('href', new URL(a.getAttribute('href'), url).href); });
      $$('img[src]', art).forEach(function (i) { i.setAttribute('src', new URL(i.getAttribute('src'), url).href); });
      var srcRow = doc.querySelector('main .src');
      body.innerHTML = ''; body.appendChild(art); if (srcRow) body.appendChild(srcRow);
      body.scrollTop = 0; openLink.href = url; drawer.hidden = false; scrim.hidden = false; document.body.style.overflow = 'hidden';
      backBtn.hidden = stack.length < 2;
      if (push) history.pushState({ drawer: url }, '', location.href);
    }).catch(function () { location.href = url; });
  }
  if (drawer) {
    document.addEventListener('click', function (ev) {
      var a = ev.target.closest('a[data-drug]');
      if (!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault();
      var url = new URL(a.getAttribute('href'), location.href).href;
      stack.push(url); showDrawer(url, stack.length === 1);
    });
    $('#drawer-close').addEventListener('click', function () { closeDrawer(true); });
    scrim.addEventListener('click', function () { closeDrawer(true); });
    backBtn.addEventListener('click', function () { stack.pop(); if (stack.length) showDrawer(stack[stack.length - 1], false); else closeDrawer(true); });
    window.addEventListener('popstate', function () { if (!drawer.hidden) closeDrawer(false); });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape' && !drawer.hidden) closeDrawer(true); });
  }
})();
