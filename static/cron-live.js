// ── Cron Live Activity Tracker (sidebar widget) ──────────────────────────
// Polls /api/cron/live-status every 10s and shows a compact status bar
// in the sidebar footer. Displays: "⚡ N running · last: ✔ job_name"

let _cronLivePollTimer = null;
const _CRON_LIVE_POLL_MS = 10000;

function startCronLiveTracker() {
  if (_cronLivePollTimer) return;
  _ensureCronLiveContainer();
  _cronLivePollTimer = setInterval(() => {
    api('/api/cron/live-status').then(_renderCronLiveStatus).catch(() => {});
  }, _CRON_LIVE_POLL_MS);
  // Immediate first fetch
  setTimeout(() => {
    api('/api/cron/live-status').then(_renderCronLiveStatus).catch(() => {});
  }, 500);
}

function stopCronLiveTracker() {
  if (_cronLivePollTimer) {
    clearInterval(_cronLivePollTimer);
    _cronLivePollTimer = null;
  }
}

function _ensureCronLiveContainer() {
  if ($('cronLiveStatus')) return;
  var sidebar = $('sessionList') || document.querySelector('.sidebar-list');
  if (!sidebar) return;
  var div = document.createElement('div');
  div.id = 'cronLiveStatus';
  div.className = 'cron-live-status';
  div.innerHTML = '<span class="cron-live-icon">⚡</span><span class="cron-live-text"></span>';
  sidebar.parentElement.appendChild(div);
}

function _renderCronLiveStatus(data) {
  var el = $('cronLiveStatus');
  if (!el) return;
  var textEl = el.querySelector('.cron-live-text');
  if (!textEl) return;

  var active = data.active || {};
  var lastRun = data.last_run || {};
  var activeCount = data.active_count || 0;

  var parts = [];

  if (activeCount > 0) {
    var names = [];
    for (var k in active) {
      var a = active[k];
      var name = (a.name || k).substring(0, 30);
      var elapsed = Math.round(((Date.now() / 1000) - (a.started_at || 0)) / 60);
      names.push(name + ' (' + elapsed + 'm)');
    }
    parts.push('⚡ ' + names.join(', '));
    el.classList.add('active');
  } else {
    el.classList.remove('active');
  }

  // Last finished job
  var lastKey = Object.keys(lastRun)[0];
  if (lastKey) {
    var lr = lastRun[lastKey];
    var icon = lr.success ? '✔' : '✘';
    var dur = lr.duration_s ? Math.round(lr.duration_s) + 's' : '';
    parts.push(icon + ' ' + (lr.name || lastKey).substring(0, 25) + (dur ? ' ' + dur : ''));
    el.classList.toggle('last-fail', !lr.success);
  }

  if (parts.length === 0) {
    textEl.textContent = 'no cron activity';
    el.classList.remove('active', 'last-fail');
  } else {
    textEl.textContent = parts.join('  ·  ');
  }
}

// Auto-start if cron panel is open
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', function() {
    startCronLiveTracker();
  });
}
