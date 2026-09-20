/* audit-a11y.js - assertions this site is supposed to keep passing.
   No build step and no dependencies: paste into the DevTools console on any page, or
   run it over a page list from a headless driver. Returns a report and logs failures.

     copy(JSON.stringify(await auditA11y(), null, 1))

   Checks, per WCAG 2.2 AA:
     contrast   1.4.3  every painted text node >= 4.5:1 (3:1 for large text)
     targets    2.5.8  every interactive element >= 24x24 CSS px
     headings   1.3.1  no skipped levels, no duplicate concept across levels
     orphans    -      no section heading with nothing visible under it
     focus      2.4.3  nothing focusable that is positioned off screen           */
(function (root) {
  'use strict';
  var LARGE_PX = 24, LARGE_BOLD_PX = 18.66;

  // getComputedStyle resolves color-mix() to color(srgb r g b / a) with 0-1 channels,
  // which a naive number scrape reads as near-black and reports as a false failure.
  function parseRGB(s) {
    s = String(s);
    var m = s.match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\s*\)/);
    if (m) return [+m[1] * 255, +m[2] * 255, +m[3] * 255, m[4] === undefined ? 1 : +m[4]];
    if (/^(rgba?|color|lab|lch|oklab|oklch|hsl)/.test(s) && !/^rgba?\(/.test(s)) {
      var cv = parseRGB.cv || (parseRGB.cv = document.createElement('canvas').getContext('2d'));
      cv.fillStyle = '#000'; cv.fillStyle = s;                    // invalid values leave #000
      var hex = cv.fillStyle;
      if (/^#/.test(hex)) return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16), 1];
      s = hex;
    }
    var n = s.match(/-?[\d.]+/g) || [];
    return [+n[0] || 0, +n[1] || 0, +n[2] || 0, n.length > 3 ? +n[3] : 1];
  }
  function lin(c) { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
  function lum(c) { return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]); }
  function over(fg, bg) { var a = fg[3]; return [0, 1, 2].map(function (i) { return fg[i] * a + bg[i] * (1 - a); }).concat([1]); }

  // walk up for the first opaque backdrop, compositing any translucent layers on the way
  function backdrop(el) {
    var stack = [], n = el;
    while (n && n.nodeType === 1) {
      var c = parseRGB(getComputedStyle(n).backgroundColor);
      if (c[3] > 0) { stack.push(c); if (c[3] === 1) break; }
      n = n.parentElement;
    }
    var bg = [255, 255, 255, 1];
    if (stack.length && stack[stack.length - 1][3] === 1) bg = stack.pop();
    else { var b = parseRGB(getComputedStyle(document.body).backgroundColor); if (b[3] === 1) bg = b; }
    while (stack.length) bg = over(stack.pop(), bg);
    return bg;
  }
  function ratio(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }

  function painted(el) {
    if (!el || el.nodeType !== 1) return false;
    var r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    if (el.checkVisibility) return el.checkVisibility({ checkVisibilityCSS: true, contentVisibilityAuto: true, opacityProperty: true });
    var cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && +cs.opacity > 0.05;
  }
  function label(el) {
    return (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
      (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '')).slice(0, 70);
  }

  root.auditA11y = function auditA11y() {
    var fail = { contrast: [], targets: [], headings: [], orphans: [], focus: [] }, notes = [];

    // ---- 1.4.3 contrast, on real text nodes rather than on selectors
    var seen = {}, walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), n;
    while ((n = walker.nextNode())) {
      if (!n.textContent.trim()) continue;
      var el = n.parentElement;
      if (!el || el.closest('svg') || !painted(el)) continue;
      var cs = getComputedStyle(el), size = parseFloat(cs.fontSize);
      var large = size >= LARGE_PX || (size >= LARGE_BOLD_PX && +cs.fontWeight >= 700);
      var bg = backdrop(el), c = Math.round(ratio(over(parseRGB(cs.color), bg), bg) * 100) / 100;
      var need = large ? 3 : 4.5;
      if (c < need) {
        var k = label(el) + '|' + size;
        if (!seen[k] || seen[k].contrast > c) seen[k] = { el: label(el), px: Math.round(size * 100) / 100, contrast: c, need: need, sample: n.textContent.trim().slice(0, 36) };
      }
    }
    Object.keys(seen).forEach(function (k) { fail.contrast.push(seen[k]); });
    fail.contrast.sort(function (a, b) { return a.contrast - b.contrast; });

    // ---- 2.5.8 target size
    var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';
    var inter = [].slice.call(document.querySelectorAll(FOCUSABLE)).filter(painted);
    inter.forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width < 24 || r.height < 24) fail.targets.push({ el: label(el), w: Math.round(r.width), h: Math.round(r.height), text: (el.textContent || '').trim().slice(0, 28) });
    });

    // ---- 2.4.3 nothing focusable parked off screen. A skip link is meant to sit off
    // screen until it is focused, so ask the element rather than guess: focus it and see
    // whether it comes back.
    var wasFocused = document.activeElement;
    inter.forEach(function (el) {
      if (el.closest('[inert]')) return;
      var r = el.getBoundingClientRect();
      if (!(r.right < 0 || r.left >= innerWidth)) return;
      try { el.focus({ preventScroll: true }); } catch (e) { return; }
      var r2 = el.getBoundingClientRect();
      if (!(r2.right < 0 || r2.left >= innerWidth)) return;         // comes back on focus: correct
      // :focus-visible cannot be set programmatically, so an element that only returns
      // under :focus-visible (the skip-link pattern) cannot be judged from here.
      if (!el.matches(':focus-visible')) { notes.push({ el: label(el), why: 'off screen; needs a real Tab press to verify' }); return; }
      fail.focus.push({ el: label(el), x: Math.round(r2.left) });
    });
    if (wasFocused && wasFocused.focus) { try { wasFocused.focus({ preventScroll: true }); } catch (e) {} }

    // ---- 1.3.1 heading order, and one concept per level
    var hs = [].slice.call(document.querySelectorAll('main :is(h1,h2,h3,h4,h5,h6)')).filter(painted)
      .map(function (h) { return { lv: +h.tagName[1], t: h.textContent.trim().replace(/\s+/g, ' ') }; });
    hs.forEach(function (h, i) {
      if (i && h.lv - hs[i - 1].lv > 1) fail.headings.push({ kind: 'skipped level', from: hs[i - 1].lv, to: h.lv, t: h.t.slice(0, 44) });
      if (/:\s*$/.test(h.t)) fail.headings.push({ kind: 'trailing colon', t: h.t.slice(0, 44) });
    });
    var byText = {};
    hs.forEach(function (h) { (byText[h.t.toLowerCase()] = byText[h.t.toLowerCase()] || {})[h.lv] = 1; });
    Object.keys(byText).forEach(function (t) {
      var lvs = Object.keys(byText[t]);
      if (lvs.length > 1) fail.headings.push({ kind: 'same concept at several levels', levels: lvs, t: t.slice(0, 44) });
    });

    // ---- a heading standing over nothing
    [].slice.call(document.querySelectorAll('section, .bl, .dtl, .money, .alt-blk')).filter(painted).forEach(function (sec) {
      var h = sec.querySelector(':scope > h2, :scope > h3, :scope > h4, :scope > .money-hd, :scope > .alt-h');
      if (!h || !painted(h)) return;
      var txt = 0, w = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT), t;
      while ((t = w.nextNode())) { if (h.contains(t) || !painted(t.parentElement)) continue; txt += t.textContent.trim().length; }
      if (!txt) fail.orphans.push({ el: label(sec), heading: h.textContent.trim().slice(0, 40) });
    });

    var total = Object.keys(fail).reduce(function (s, k) { return s + fail[k].length; }, 0);
    var report = { notes: notes, url: location.pathname, theme: getComputedStyle(document.body).backgroundColor, viewport: innerWidth + 'x' + innerHeight, failures: total, detail: fail };
    if (total) console.warn('auditA11y: ' + total + ' failure(s)', fail); else console.log('auditA11y: clean');
    return report;
  };
})(window);
