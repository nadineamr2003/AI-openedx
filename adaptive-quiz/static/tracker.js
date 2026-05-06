/*
 * Open edX behavior tracker.
 *
 * Session rules:
 *   - new tab          → new session (sessionStorage is per-tab)
 *   - different user   → new session (username switch detected on each event)
 *   - logout + re-login→ new session (username goes anonymous, then back)
 *   - 30 min idle      → new session (no recorded event for that long)
 */
(function () {
  if (window.__edxTrackerLoaded) return;
  window.__edxTrackerLoaded = true;

  var BACKEND_URL          = 'http://localhost:8100/api/analytics/events';
  var FLUSH_INTERVAL_MS    = 5000;
  var HOVER_THRESHOLD_MS   = 1500;
  var HOVER_MAX_MOVEMENT   = 20;
  var CLICK_HOVER_SUPPRESS = 250;
  var IDLE_TIMEOUT_MS      = 30 * 60 * 1000;   // 30 minutes
  var SESSION_KEY          = 'edx_tracker_session_v2';
  var BUFFER_SIZE_LIMIT    = 50;

  var BUTTON_SELECTOR =
    'button, a, [role="button"], [role="menuitem"], input[type="submit"], input[type="button"]';

  // ---- username (re-read on every event) -------------------------------
  function readUsernameFromForm() {
    // Pick up the username being typed during registration / login.
    var selectors = [
      'input[name="username"]',
      'input[id="username"]',
      'input[id="register-username"]',
      'input[id="reg-username"]',
      'input[id="login-email"]',
      'input[autocomplete="username"]'
    ];
    for (var i = 0; i < selectors.length; i++) {
      try {
        var el = document.querySelector(selectors[i]);
        if (el && el.value && el.value.trim() && el.value.trim() !== 'null') {
          return el.value.trim();
        }
      } catch (e) {}
    }
    return null;
  }

  function readUsername() {
    if (window.OPENEDX_USERNAME && window.OPENEDX_USERNAME !== 'anonymous') return window.OPENEDX_USERNAME;
    try { if (window.parent && window.parent !== window && window.parent.OPENEDX_USERNAME) return window.parent.OPENEDX_USERNAME; } catch (e) {}
    try {
      var m = (document.cookie || '').match(/username[^A-Za-z0-9_]+([A-Za-z0-9_.@-]+)/i);
      if (m && m[1] && m[1] !== 'null') return m[1];
    } catch (e) {}
    var formUser = readUsernameFromForm();
    if (formUser) return formUser;
    return 'anonymous';
  }

  // ---- session helpers -------------------------------------------------
  function newId() {
    return (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'sess-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }

  // Returns {id, user} for the active session, creating a new one when:
  //   - no session exists in this tab, OR
  //   - the username has changed since the session was created, OR
  //   - the last recorded event was more than IDLE_TIMEOUT_MS ago.
  // Cookie helpers — scoped to the parent domain so the session is shared
  // between local.openedx.io and apps.local.openedx.io.
  function parentDomain() {
    var parts = location.hostname.split('.');
    if (parts.length <= 2) return location.hostname;
    return '.' + parts.slice(-3).join('.');
  }
  function readSessionCookie() {
    try {
      var m = (document.cookie || '').match(new RegExp(SESSION_KEY + '=([^;]+)'));
      return m ? JSON.parse(decodeURIComponent(m[1])) : null;
    } catch (e) { return null; }
  }
  function writeSessionCookie(data) {
    // No Expires/Max-Age = session cookie (cleared on browser close).
    document.cookie = SESSION_KEY + '=' + encodeURIComponent(JSON.stringify(data)) +
      '; path=/; domain=' + parentDomain() + '; SameSite=Lax';
  }

  function getActiveSession(currentUser) {
    var now = Date.now();
    var data = readSessionCookie();

    var sameUser   = data && data.user === currentUser;
    var withinIdle = data && (now - (data.last || 0)) <= IDLE_TIMEOUT_MS;

    if (data && sameUser && withinIdle) {
      data.last = now;
      writeSessionCookie(data);
      return data;
    }

    var fresh = { id: newId(), user: currentUser, last: now };
    writeSessionCookie(fresh);
    return fresh;
  }

  // ---- label extraction ------------------------------------------------
  function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim().slice(0, 100); }

  function visibleText(el) {
    var parts = [], children = el.childNodes;
    for (var i = 0; i < children.length; i++) {
      var node = children[i];
      if (node.nodeType === 3) parts.push(node.textContent);
      else if (node.nodeType === 1) {
        if (node.matches('svg, i, .icon, .sr-only, .visually-hidden, [aria-hidden="true"]')) continue;
        parts.push(node.innerText || node.textContent || '');
      }
    }
    return clean(parts.join(' '));
  }

  function buttonLabelFor(el) {
    if (!el) return '(unlabeled)';
    if (el.dataset && el.dataset.action) return 'action:' + el.dataset.action;
    var aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    if (el.value && (el.tagName === 'INPUT' || el.tagName === 'BUTTON')) {
      var v = clean(el.value);
      if (v) return v;
    }
    if (el.id && !/^(react-|mui-|css-|chakra-|radix-|headlessui-|undefined)/.test(el.id)) return '#' + el.id;
    var text = visibleText(el);
    if (text) return text;
    var cls = (el.className && typeof el.className === 'string')
      ? el.className.split(/\s+/).filter(Boolean)[0] : '';
    return cls ? el.tagName.toLowerCase() + '.' + cls : '(unlabeled)';
  }

  // ---- buffer + record -------------------------------------------------
  var buffer = [];
  function record(buttonLabel, action) {
    if (!buttonLabel) buttonLabel = '(unlabeled)';
    var user = readUsername();
    var sess = getActiveSession(user);
    buffer.push({
      username:   user,
      session_id: sess.id,
      timestamp:  new Date().toISOString(),
      button:     buttonLabel,
      action:     action
    });
    if (buffer.length >= BUFFER_SIZE_LIMIT) flush();
  }

  // ---- click capture ---------------------------------------------------
  var lastClickAt = 0, lastClickedBtn = null;
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest(BUTTON_SELECTOR);
    if (!btn) return;
    record(buttonLabelFor(btn), 'clicked');
    lastClickAt = Date.now();
    lastClickedBtn = btn;
    if (btn === hoverTarget) hoverClicked = true;
  }, true);

  // ---- hover capture (record at exit; dwell >= MIN_DWELL_MS, not clicked) ----
  var HOVER_MIN_DWELL_MS = 500;
  var hoverTarget = null;
  var hoverStart  = 0;
  var hoverClicked = false;

  document.addEventListener('mouseover', function (e) {
    var btn = e.target.closest && e.target.closest(BUTTON_SELECTOR);
    if (!btn) return;
    // Skip if we're moving between nested children of the same button
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;

    hoverTarget  = btn;
    hoverStart   = Date.now();
    hoverClicked = false;
  }, true);

  document.addEventListener('mouseout', function (e) {
    var btn = e.target.closest && e.target.closest(BUTTON_SELECTOR);
    if (!btn || btn !== hoverTarget) return;
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;  // still inside

    var dwell = Date.now() - hoverStart;
    if (dwell >= HOVER_MIN_DWELL_MS && !hoverClicked) {
      record(buttonLabelFor(btn), 'hovered');
    }
    hoverTarget = null;
    hoverStart = 0;
    hoverClicked = false;
  }, true);


  // ---- flush -----------------------------------------------------------
  function flush() {
    if (buffer.length === 0) return;
    var payload = JSON.stringify({ events: buffer.splice(0) });
    if (navigator.sendBeacon) {
      navigator.sendBeacon(BACKEND_URL, new Blob([payload], { type: 'application/json' }));
    } else {
      fetch(BACKEND_URL, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: payload, keepalive: true
      }).catch(function () {});
    }
  }

  setInterval(flush, FLUSH_INTERVAL_MS);
  window.addEventListener('beforeunload', flush);
  document.addEventListener('visibilitychange', function () { if (document.hidden) flush(); });
})();
