/* Media Saver share page: RESOLVING → DOWNLOADING → DONE | ERROR. Zero decisions, zero questions. */
(function () {
  'use strict';
  var FRIENDLY = {
    unsupported_url: "That link isn't a Twitter/X or TikTok post.",
    not_found: 'Post not found.',
    private: 'Private post — login required.',
    no_media: 'That post has no media to save.',
    extract_failed: "Couldn't extract media from that post.",
    rate_limited: 'Too many saves — wait a minute and retry.',
    timeout: 'The site took too long to respond.',
    unauthorized: 'Not logged in.',
    network: 'No connection — check your network and retry.'
  };
  var STAGGER_MS = 400;
  var CLOSE_AFTER_MS = 2500;

  var $ = function (id) { return document.getElementById(id); };
  var statusEl = $('status'), detailEl = $('detail'), itemsEl = $('items'), actionsEl = $('actions'), hintEl = $('hint');

  function settings() {
    try { return JSON.parse(localStorage.getItem('ms.settings') || '{}'); } catch (e) { return {}; }
  }

  function sharedInput() {
    var p = new URLSearchParams(location.search);
    var url = p.get('url') || '', text = p.get('text') || '', title = p.get('title') || '';
    var input = (url + ' ' + text).trim();
    if (!/https?:\/\//i.test(input) && /https?:\/\//i.test(title)) input = title; // some apps put the link in title
    return input;
  }

  function setState(html, cls) {
    statusEl.className = cls || '';
    statusEl.innerHTML = html;
  }

  function showError(code, message) {
    setState('✕ ' + (FRIENDLY[code] || message || 'Something went wrong.'), 'err');
    detailEl.textContent = message && !FRIENDLY[code] ? '' : (code === 'unsupported_url' || code === 'network' ? '' : (message || ''));
    actionsEl.classList.remove('hidden');
    hintEl.textContent = '';
  }

  function loginRedirect() {
    location.replace('/login?next=' + encodeURIComponent(location.pathname + location.search));
  }

  function triggerDownload(href, filename) {
    var a = document.createElement('a');
    a.href = href; a.download = filename || ''; a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { a.remove(); }, 1000);
  }

  function renderItems(items) {
    itemsEl.innerHTML = '';
    return items.map(function (it) {
      var li = document.createElement('li');
      var name = document.createElement('span'); name.textContent = it.filename;
      var mark = document.createElement('span'); mark.textContent = '…';
      li.appendChild(name); li.appendChild(mark); itemsEl.appendChild(li);
      return mark;
    });
  }

  function finish(resp) {
    setState('Saved ✓', 'ok');
    detailEl.textContent = '@' + resp.author + ' · ' + resp.summary;
    setTimeout(function () {
      try { window.close(); } catch (e) { /* best effort */ }
      setTimeout(function () {
        hintEl.textContent = 'Downloads running — you can go back now.';
      }, 300);
    }, CLOSE_AFTER_MS);
  }

  function startDownloads(resp) {
    var s = settings();
    var items = resp.items || [];
    if (!items.length) return showError('no_media');
    setState('<span class="spin"></span>Downloading…');
    detailEl.textContent = '@' + resp.author + ' · ' + resp.summary;
    if (s.zip_multi && items.length > 1 && resp.zip) {
      var marks = renderItems([{ filename: 'zip · ' + items.length + ' files' }]);
      triggerDownload(resp.zip, '');
      marks[0].textContent = '✓'; marks[0].className = 'ok';
      return finish(resp);
    }
    if (items.length > 1) hintEl.textContent = 'If Chrome asks, allow multiple downloads (remembered).';
    var marks = renderItems(items);
    items.forEach(function (it, i) {
      setTimeout(function () {
        triggerDownload(it.dl, it.filename);
        marks[i].textContent = '✓'; marks[i].className = 'ok';
        if (i === items.length - 1) finish(resp);
      }, i * STAGGER_MS);
    });
  }

  function resolve() {
    var input = sharedInput();
    actionsEl.classList.add('hidden');
    itemsEl.innerHTML = ''; detailEl.textContent = ''; hintEl.textContent = '';
    setState('<span class="spin"></span>Saving…');
    if (!input) return showError('unsupported_url', 'No link found in what was shared.');
    var slow = setTimeout(function () { setState('<span class="spin"></span>Converting gif…'); }, 3000);
    var s = settings();
    var body = { url: input, options: {} };
    if (typeof s.gif_keep_mp4 === 'boolean') body.options.gif_keep_mp4 = s.gif_keep_mp4;
    fetch('/v1/resolve', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) {
      clearTimeout(slow);
      if (r.status === 401) return loginRedirect();
      return r.json().then(function (data) {
        if (!r.ok) return showError(data.error || 'extract_failed', data.message);
        startDownloads(data);
      }, function () { showError('extract_failed', 'Bad response from server (' + r.status + ').'); });
    }).catch(function () { clearTimeout(slow); showError('network'); });
  }

  $('retry').addEventListener('click', resolve);
  if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(function () {}); }
  resolve();
})();
