/* Media Saver settings/status page. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var KEY = 'ms.settings';
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
  function save(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} }

  // settings
  var s = load();
  $('opt-gif-mp4').checked = s.gif_keep_mp4 !== false;
  $('opt-zip').checked = !!s.zip_multi;
  $('opt-gif-mp4').addEventListener('change', function () { s.gif_keep_mp4 = this.checked; save(s); });
  $('opt-zip').addEventListener('change', function () { s.zip_multi = this.checked; save(s); });

  // login state (we are on / so the cookie passed the server check)
  $('st-login').textContent = '✓'; $('st-login').className = 'status ok';

  // health
  fetch('/v1/health', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (h) {
    $('st-health').textContent = h.ok ? '✓' : '✕'; $('st-health').className = 'status ' + (h.ok ? 'ok' : 'err');
    $('health-detail').textContent = 'yt-dlp ' + h.ytdlp_version + (h.ffmpeg ? '' : ' · ffmpeg missing');
  }).catch(function () { $('st-health').textContent = '✕'; $('st-health').className = 'status err'; });

  // install state
  var standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  var deferred = null;
  function setInstall(state, text) { $('st-install').textContent = state; $('st-install').className = 'status ' + (state === '✓' ? 'ok' : 'muted'); $('install-text').textContent = text; }
  if (standalone) setInstall('✓', 'Installed');
  else setInstall('○', 'Not installed — use Chrome menu → Add to Home screen');
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault(); deferred = e; $('install-btn').classList.remove('hidden');
  });
  $('install-btn').addEventListener('click', function () {
    if (!deferred) return; deferred.prompt();
    deferred.userChoice.then(function () { deferred = null; $('install-btn').classList.add('hidden'); });
  });
  window.addEventListener('appinstalled', function () { setInstall('✓', 'Installed'); });

  // test with a link → run the real share flow
  $('test-form').addEventListener('submit', function (e) {
    e.preventDefault();
    location.href = '/share?url=' + encodeURIComponent($('test-url').value.trim());
  });

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(function () {});
})();
