/* ── Adaptive Quiz XBlock — quiz.js ────────────────────────────────── */

function AdaptiveQuizXBlock(runtime, element, initArgs) {

  var MAX_Q = initArgs.max_questions || 10;
  var DISPLAY_NAME = initArgs.display_name || 'Adaptive Quiz';

  var urlStart = runtime.handlerUrl(element, 'start_session');
  var urlSubmit = runtime.handlerUrl(element, 'submit_answer');
  var urlExplain = runtime.handlerUrl(element, 'explain_simpler');
  var urlSimilar = runtime.handlerUrl(element, 'similar_question');
  var urlProgress = runtime.handlerUrl(element, 'get_progress');
  var urlGetContent = runtime.handlerUrl(element, 'get_content');
  var urlGetCourses = runtime.handlerUrl(element, 'get_courses');
  var urlSessionHistory = runtime.handlerUrl(element, 'get_session_history');

  var state = {
    currentQuestion: null,
    answered: false,
    questionStart: null,
    questionsSeenSoFar: initArgs.questions_seen || 0,
    sessionScore: initArgs.session_score || 0,
    lastTopic: '—',
    lastMasteryPct: 50,
    lastDifficulty: 3,
    maxQuestionsCurrent: initArgs.max_questions || 10,
    dashboardOrigin: 'start',
  };

  function $(sel) { return element.querySelector(sel); }

  var SCREENS = ['start', 'loading', 'question', 'results', 'dashboard', 'course', 'content'];

  function showScreen(name) {
    SCREENS.forEach(function (s) {
      var el = element.querySelector('#aq-screen-' + s);
      if (el) el.classList.toggle('aq-hidden', s !== name);
    });
  }

  function setLoading(msg) {
    showScreen('loading');
    var msgEl = $('#aq-loading-msg');
    if (msgEl) msgEl.textContent = msg || 'Loading…';
  }

  var DIFF_LABEL = {
    1: 'Very Easy',
    2: 'Easy',
    3: 'Medium',
    4: 'Hard',
    5: 'Very Hard'
  };

  var DIFF_CLASS = {
    1: 'diff-very-easy',
    2: 'diff-easy',
    3: 'diff-medium',
    4: 'diff-hard',
    5: 'diff-very-hard'
  };

  // ── Pill selector ──────────────────────────────────────────────────
  var pillSelector = $('#aq-pill-selector');
  if (pillSelector) {
    var pills = pillSelector.querySelectorAll('.aq-pill');
    pills.forEach(function (pill) {
      pill.addEventListener('click', function () {
        pills.forEach(function (p) { p.classList.remove('aq-pill-active'); });
        pill.classList.add('aq-pill-active');
        var input = $('#aq-question-count');
        if (input) input.value = pill.getAttribute('data-value');
      });
    });
  }

  // ── Course picker ──────────────────────────────────────────────────
  var selectedCourseId = '';
  var selectedCourseName = '';
  var selectedContentIds = [];
  var pickerMode = 'quiz';
  var allContentItems = [];

  function loadCoursePicker() {
    setLoading('Loading available courses…');
    jQuery.ajax({
      type: 'POST', url: urlGetCourses,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (!data.success || !data.courses || data.courses.length === 0) {
          alert('No courses available yet.');
          showScreen('start');
          return;
        }
        renderCoursePicker(data.courses);
      },
      error: function () { alert('Could not load courses.'); showScreen('start'); }
    });
  }

  function renderCoursePicker(courses) {
    var list = $('#aq-course-list');
    if (!list) { showScreen('start'); return; }
    list.innerHTML = '';
    courses.forEach(function (course, index) {
      var cid = course.course_id;
      var cname = course.course_name || cid;
      var label = document.createElement('label');
      label.innerHTML =
        '<input type="radio" name="aq-course-choice" value="' + cid + '" data-course-name="' + cname + '"' + (index === 0 ? ' checked' : '') + '>' +
        '<span><strong>' + cid + '</strong>' +
        (cname !== cid ? '<span style="color:#6B7280;font-size:.82rem;margin-left:8px;">' + cname + '</span>' : '') +
        '</span>';
      list.appendChild(label);
    });
    showScreen('course');
  }

  // ── Content picker ─────────────────────────────────────────────────
  function loadContentPicker(courseId) {
    setLoading('Loading your course content…');
    jQuery.ajax({
      type: 'POST', url: urlGetContent,
      data: JSON.stringify({ selected_course_id: courseId }),
      contentType: 'application/json',
      success: function (data) {
        if (!data.success || !data.items || data.items.length === 0) {
          alert('No content found for this course yet.');
          showScreen('course');
          return;
        }
        allContentItems = data.items || [];
        renderContentPicker(allContentItems);
        initContentFilters(allContentItems);
      },
      error: function () { alert('Could not load course content.'); showScreen('course'); }
    });
  }

  function renderContentPicker(items) {
    var list = $('#aq-content-list');
    if (!list) { showScreen('start'); return; }
    list.innerHTML = '';
    if (!items || items.length === 0) {
      list.innerHTML = '<p style="color:#6B7280;font-size:.88rem;padding:16px 0;">No content matches the selected filters.</p>';
      showScreen('content');
      return;
    }
    items.forEach(function (item) {
      var label = document.createElement('label');
      label.innerHTML =
        '<input type="checkbox" value="' + item.title + '">' +
        '<span>' +
        '<strong style="font-size:.9rem;">' + item.title + '</strong>' +
        '<span class="aq-content-meta"> Week ' + item.week + ' · ' + item.content_type + '</span>' +
        '</span>';
      list.appendChild(label);
    });
    showScreen('content');
  }

  function initContentFilters(items) {
    var weekSel = $('#aq-filter-week');
    var typeSel = $('#aq-filter-type');
    var searchInp = $('#aq-filter-search');
    if (!weekSel || !typeSel || !searchInp) return;

    var weeks = Array.from(new Set(items.map(function (i) { return i.week; }))).sort(function (a, b) { return a - b; });
    weekSel.innerHTML = '<option value="">All weeks</option>';
    weeks.forEach(function (w) {
      var o = document.createElement('option');
      o.value = String(w); o.textContent = 'Week ' + w;
      weekSel.appendChild(o);
    });

    var types = Array.from(new Set(items.map(function (i) { return i.content_type; }))).sort();
    typeSel.innerHTML = '<option value="">All types</option>';
    types.forEach(function (t) {
      var o = document.createElement('option');
      o.value = t; o.textContent = t.charAt(0).toUpperCase() + t.slice(1);
      typeSel.appendChild(o);
    });

    weekSel.onchange = typeSel.onchange = searchInp.oninput = applyFilters;
  }

  function applyFilters() {
    var week = ($('#aq-filter-week') || {}).value || '';
    var type = ($('#aq-filter-type') || {}).value || '';
    var search = (($('#aq-filter-search') || {}).value || '').trim().toLowerCase();
    var filtered = allContentItems.filter(function (item) {
      return (!week || String(item.week) === week) &&
        (!type || item.content_type === type) &&
        (!search || (item.title || '').toLowerCase().indexOf(search) !== -1);
    });
    renderContentPicker(filtered);
  }

  // ── Question rendering ──────────────────────────────────────────────
  function updateHeader(question, seenNow) {
    var topicBadge = $('#aq-badge-topic');
    var diffBadge = $('#aq-badge-diff');
    var counter = $('#aq-counter');
    var progress = $('#aq-progress-bar');
    if (topicBadge) topicBadge.textContent = question.topic || 'General';
    if (counter) counter.textContent = (seenNow + 1) + ' / ' + state.maxQuestionsCurrent;
    if (diffBadge) {
      var d = question.difficulty || 3;
      diffBadge.textContent = DIFF_LABEL[d] || 'Medium';
      diffBadge.className = 'aq-tag aq-tag-diff ' + (DIFF_CLASS[d] || '');
    }
    if (progress)
      progress.style.width = Math.round((seenNow / state.maxQuestionsCurrent) * 100) + '%';
  }

  function renderQuestion(resp) {
    if (!resp || !resp.success || !resp.question) {
      alert('Could not load question: ' + (resp && resp.error ? resp.error : 'Unknown error'));
      showScreen('start');
      return;
    }
    if (resp.max_questions) state.maxQuestionsCurrent = resp.max_questions;

    var q = resp.question;
    state.currentQuestion = q;
    state.answered = false;
    state.questionStart = Date.now();

    updateHeader(q, resp.questions_seen || state.questionsSeenSoFar);

    var qtEl = $('#aq-question-text');
    if (qtEl) qtEl.textContent = q.question;

    ['A', 'B', 'C', 'D'].forEach(function (key) {
      var btn = $('#aq-opt-' + key);
      if (!btn) return;
      var textEl = btn.querySelector('.aq-opt-text');
      if (textEl) textEl.textContent = q.options[key] || '';
      btn.className = 'aq-opt';
      btn.disabled = false;
      btn.onclick = function () { handleOptionClick(key); };
    });

    var fb = $('#aq-feedback');
    if (fb) fb.classList.add('aq-hidden');

    showScreen('question');
  }

  function handleOptionClick(selectedKey) {
    if (state.answered) return;
    state.answered = true;

    $('#aq-opt-' + selectedKey).classList.add('selected');
    ['A', 'B', 'C', 'D'].forEach(function (k) {
      var b = $('#aq-opt-' + k);
      if (b) b.disabled = true;
    });

    var timeSpentMs = Date.now() - (state.questionStart || Date.now());
    jQuery.ajax({
      type: 'POST', url: urlSubmit,
      data: JSON.stringify({ selected_answer: selectedKey, time_spent_ms: timeSpentMs }),
      contentType: 'application/json',
      success: function (data) { renderFeedback(data, selectedKey); },
      error: function () { alert('Network error submitting answer.'); }
    });
  }

  function renderFeedback(data, selectedKey) {
    if (!data || !data.success) {
      alert('Error: ' + (data && data.error ? data.error : 'Unknown'));
      return;
    }

    state.questionsSeenSoFar = data.questions_seen;
    state.sessionScore = data.session_score;
    if (data.max_questions) state.maxQuestionsCurrent = data.max_questions;

    state.lastTopic = (state.currentQuestion && state.currentQuestion.topic) || 'General';
    state.lastMasteryPct = Math.round((data.updated_mastery || 0.5) * 100);
    state.lastDifficulty = data.next_difficulty || 3;

    var correct = data.correct_answer;
    ['A', 'B', 'C', 'D'].forEach(function (k) {
      var b = $('#aq-opt-' + k);
      if (!b) return;
      if (k === correct) b.classList.add('correct');
      if (k === selectedKey && k !== correct) b.classList.add('incorrect');
    });

    // Feedback banner
    var banner = $('#aq-feedback-banner');
    var icon = $('#aq-feedback-icon');
    var label = $('#aq-feedback-label');
    if (banner) {
      banner.className = 'aq-feedback-banner ' + (data.is_correct ? 'correct' : 'incorrect');
    }
    if (icon) icon.textContent = data.is_correct ? '✓' : '✕';
    if (label) label.textContent = data.is_correct ? 'Correct!' : 'Incorrect';

    var expEl = $('#aq-explanation');
    if (expEl) expEl.textContent = data.explanation || '';

    var pct = Math.round((data.updated_mastery || 0.5) * 100);
    var fillEl = $('#aq-mastery-fill');
    var pctEl = $('#aq-mastery-pct');
    if (fillEl) fillEl.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';

    var supportRow = $('#aq-support-row');
    if (supportRow) {
      supportRow.innerHTML = '';
      var features = data.support_features || [];

      if (features.indexOf('explain_simpler') !== -1) {
        supportRow.appendChild(
          makeSupportBtn('💬 Simpler explanation', handleExplainSimpler)
        );
      }

      // IMPORTANT:
      // Do not allow "one more like this" after the session is already complete,
      // otherwise the frontend score can diverge from finalized backend session history.
      if (!data.session_complete && features.indexOf('one_more_like_this') !== -1) {
        supportRow.appendChild(
          makeSupportBtn('🔄 One more like this', handleSimilarQuestion)
        );
      }
    }

    var nextBtn = $('#aq-btn-next');
    if (nextBtn) {
      nextBtn.textContent = data.session_complete ? 'See Results →' : 'Next Question →';
      nextBtn.onclick = data.session_complete
        ? function () { showResults(data); }
        : function () { loadNextQuestion(); };
    }

    var fb = $('#aq-feedback');
    if (fb) fb.classList.remove('aq-hidden');
  }

  function makeSupportBtn(label, handler) {
    var btn = document.createElement('button');
    btn.className = 'aq-btn-support';
    btn.textContent = label;
    btn.onclick = handler;
    return btn;
  }

  function formatDateTime(isoString) {
    if (!isoString) return '—';
    var d = new Date(isoString);
    return d.toLocaleString([], {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit'
    });
  }

  function formatMs(ms) {
    if (!ms || ms <= 0) return '—';
    var seconds = Math.round(ms / 1000);
    if (seconds < 60) return seconds + 's';
    var mins = Math.floor(seconds / 60);
    var rem = seconds % 60;
    return mins + 'm ' + rem + 's';
  }

  function masteryStageClass(label) {
    switch (label) {
      case 'Struggling': return 'struggling';
      case 'Emerging': return 'emerging';
      case 'Developing': return 'developing';
      case 'Proficient': return 'proficient';
      case 'Mastered': return 'mastered';
      default: return 'developing';
    }
  }

  function handleExplainSimpler() {
    jQuery.ajax({
      type: 'POST', url: urlExplain,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (data.success) {
          var expEl = $('#aq-explanation');
          if (expEl) expEl.textContent = data.simpler_explanation;
        }
      }
    });
  }

  function handleSimilarQuestion() {
    if (state.questionsSeenSoFar >= state.maxQuestionsCurrent) {
      return;
    }

    setLoading('Generating a similar question…');
    jQuery.ajax({
      type: 'POST', url: urlSimilar,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (data.success) renderQuestion({ success: true, question: data.question, questions_seen: state.questionsSeenSoFar });
        else showScreen('question');
      },
      error: function () { showScreen('question'); }
    });
  }

  function loadNextQuestion() {
    setLoading('Generating your next question…');
    jQuery.ajax({
      type: 'POST', url: runtime.handlerUrl(element, 'get_question'),
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) { renderQuestion(data); },
      error: function () { alert('Could not load next question.'); showScreen('start'); }
    });
  }

  function renderSessionInsight(data) {
    var wrap = $('#aq-session-insight');
    if (!wrap) return;

    var strongest = data.strongest_topic_this_session || '';
    var weakest = data.weakest_topic_this_session || '';
    var recommendation = data.session_recommendation || '';
    var avgTime = data.avg_time_spent_ms || 0;

    if (!strongest && !weakest && !recommendation) {
      wrap.classList.add('aq-hidden');
      return;
    }

    var strongestEl = $('#aq-insight-strongest');
    var weakestEl = $('#aq-insight-weakest');
    var avgTimeEl = $('#aq-insight-avg-time');
    var recTextEl = $('#aq-recommendation-text');

    if (strongestEl) strongestEl.textContent = strongest || '—';
    if (weakestEl) weakestEl.textContent = weakest || '—';
    if (avgTimeEl) avgTimeEl.textContent = formatMs(avgTime);
    if (recTextEl) recTextEl.textContent = recommendation || 'Keep practising to reinforce your learning.';

    wrap.classList.remove('aq-hidden');
  }

  // ── Results ────────────────────────────────────────────────────────
  function showResults(data) {
    var score = data.session_score || state.sessionScore;
    var total = state.maxQuestionsCurrent;
    var pct = Math.round((score / total) * 100);
    var incorrect = total - score;

    var emojiEl = $('#aq-result-emoji');
    var msgEl = $('#aq-results-msg') || $('#aq-score-msg');
    if (emojiEl) emojiEl.textContent = pct >= 80 ? '🏆' : pct >= 60 ? '🎉' : '📚';
    if (msgEl) msgEl.textContent = pct >= 80 ? 'Excellent — you\'ve mastered this material!'
      : pct >= 60 ? 'Good work! Keep practising to improve.'
        : 'Keep going — every attempt builds mastery!';

    var numEl = $('#aq-score-num');
    var denomEl = $('#aq-score-denom');
    if (numEl) numEl.textContent = score;
    if (denomEl) denomEl.textContent = '/ ' + total;

    // Animate ring
    var ring = $('#aq-ring-fill');
    if (ring) {
      var circumference = 326.7;
      var offset = circumference - (pct / 100) * circumference;
      setTimeout(function () { ring.style.strokeDashoffset = offset; }, 100);
    }

    // Stats
    var els = {
      '#aq-summary-accuracy': pct + '%',
      '#aq-summary-incorrect': incorrect,
      '#aq-summary-topic': state.lastTopic || 'General',
      '#aq-summary-mastery': state.lastMasteryPct + '%',
      '#aq-summary-difficulty': DIFF_LABEL[state.lastDifficulty] || 'Medium'
    };
    Object.keys(els).forEach(function (sel) {
      var el = element.querySelector(sel);
      if (el) el.textContent = els[sel];
    });

    var pb = $('#aq-progress-bar');
    if (pb) pb.style.width = '100%';

    renderSessionInsight(data);

    showScreen('results');
  }

  // ── Dashboard ──────────────────────────────────────────────────────
  function renderDashboard(data) {
    if (!data || !data.success) {
      alert('Could not load progress dashboard.');
      showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
      return;
    }

    var courseLabelEl = $('#aq-dashboard-course-label');
    if (courseLabelEl)
      courseLabelEl.textContent = 'Course: ' + (selectedCourseName || data.course_id || '—');

    var backBtn = $('#aq-btn-back-results');
    if (backBtn)
      backBtn.textContent = state.dashboardOrigin === 'results' ? '← Back to Results' : '← Back to Home';

    var topicsWrap = $('#aq-dashboard-topics');
    var emptyEl = $('#aq-dashboard-empty');
    if (topicsWrap) topicsWrap.innerHTML = '';

    if (!data.has_progress || !data.topic_mastery || Object.keys(data.topic_mastery).length === 0) {
      if (emptyEl) emptyEl.classList.remove('aq-hidden');
      showScreen('dashboard');
      return;
    }
    if (emptyEl) emptyEl.classList.add('aq-hidden');

    var fields = {
      '#aq-dash-sessions': data.session_count || 0,
      '#aq-dash-total-answers': data.total_answers || 0,
      '#aq-dash-irt': data.irt_active ? 'Active' : 'Warming up',
      '#aq-dash-difficulty': DIFF_LABEL[data.current_difficulty || 3] || 'Medium'
    };
    Object.keys(fields).forEach(function (sel) {
      var el = element.querySelector(sel);
      if (el) el.textContent = fields[sel];
    });

    if (topicsWrap) {
      var mastery = data.topic_mastery || {};
      var topicLabels = data.topic_labels || {};

      var sortedTopics = Object.keys(mastery).sort(function (a, b) {
        return (mastery[a] || 0) - (mastery[b] || 0);
      });

      sortedTopics.forEach(function (topic) {
        var pct = Math.round((mastery[topic] || 0) * 100);
        var label = topicLabels[topic] || 'Developing';
        var cls = masteryStageClass(label);
        var badgeText = pct + '% · ' + label;

        var block = document.createElement('div');
        block.className = 'aq-dash-topic';
        block.innerHTML =
          '<div class="aq-dash-topic-row">' +
          '<span class="aq-dash-topic-name">' + topic + '</span>' +
          '<span class="aq-dash-topic-badge ' + cls + '">' + badgeText + '</span>' +
          '</div>' +
          '<div class="aq-dash-track">' +
          '<div class="aq-dash-fill ' + cls + '" style="width:' + pct + '%"></div>' +
          '</div>';
        topicsWrap.appendChild(block);
      });
    }

    showScreen('dashboard');
  }

  function renderSessionHistory(sessions) {
    var wrap = $('#aq-session-history-list');
    var empty = $('#aq-session-history-empty');
    if (!wrap) return;

    wrap.innerHTML = '';

    if (!sessions || sessions.length === 0) {
      if (empty) empty.classList.remove('aq-hidden');
      return;
    }

    if (empty) empty.classList.add('aq-hidden');

    sessions.forEach(function (session) {
      var score = (session.correct_answers || 0) + ' / ' + (session.target_questions || 0);
      var pct = Math.round((session.accuracy || 0) * 100);

      var card = document.createElement('div');
      card.className = 'aq-session-card';
      card.innerHTML =
        '<div class="aq-session-card-top">' +
        '<div>' +
        '<div class="aq-session-date">' + formatDateTime(session.ended_at || session.started_at) + '</div>' +
        '<div class="aq-session-meta">Completed session</div>' +
        '</div>' +
        '<div class="aq-session-score-badge">' + score + ' · ' + pct + '%</div>' +
        '</div>' +

        '<div class="aq-session-card-grid">' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">Strongest Topic</span>' +
        '<span class="aq-session-mini-value">' + (session.strongest_topic_this_session || '—') + '</span>' +
        '</div>' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">Needs Review</span>' +
        '<span class="aq-session-mini-value">' + (session.weakest_topic_this_session || '—') + '</span>' +
        '</div>' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">Avg Response Time</span>' +
        '<span class="aq-session-mini-value">' + formatMs(session.avg_time_spent_ms || 0) + '</span>' +
        '</div>' +
        '</div>' +

        '<div class="aq-session-recommendation">' +
        '<strong>Recommendation:</strong> ' + (session.recommendation || 'Keep building mastery through regular practice.') +
        '</div>';

      wrap.appendChild(card);
    });
  }

  function loadSessionHistory(courseId) {
    jQuery.ajax({
      type: 'POST',
      url: urlSessionHistory,
      data: JSON.stringify({ selected_course_id: courseId }),
      contentType: 'application/json',
      success: function (data) {
        if (data && data.success) renderSessionHistory(data.sessions || []);
        else renderSessionHistory([]);
      },
      error: function () {
        renderSessionHistory([]);
      }
    });
  }

  function loadDashboard(origin, courseId, courseName) {
    state.dashboardOrigin = origin || 'start';
    if (courseId) selectedCourseId = courseId;
    if (courseName) selectedCourseName = courseName;
    setLoading('Loading your progress…');
    jQuery.ajax({
      type: 'POST', url: urlProgress,
      data: JSON.stringify({ selected_course_id: selectedCourseId }),
      contentType: 'application/json',
      success: function (data) {
        if (!data || !data.success) {
          alert('Dashboard error: ' + ((data && data.error) ? data.error : 'Unknown'));
          showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
          return;
        }
        renderDashboard(data);
        loadSessionHistory(selectedCourseId);
      },
      error: function (xhr) {
        alert('Could not load progress. HTTP ' + xhr.status);
        showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
      }
    });
  }

  // ── Session start ───────────────────────────────────────────────────
  function startSessionWithIds(ids, courseId) {
    var countInput = $('#aq-question-count');
    var chosenCount = countInput ? parseInt(countInput.value, 10) : MAX_Q;
    state.questionsSeenSoFar = 0;
    state.sessionScore = 0;
    state.lastTopic = '—';
    state.lastMasteryPct = 50;
    state.lastDifficulty = 3;
    setLoading('Preparing your adaptive quiz…');
    jQuery.ajax({
      type: 'POST', url: urlStart,
      data: JSON.stringify({ question_count: chosenCount, selected_course_id: courseId, content_ids: ids }),
      contentType: 'application/json',
      success: function (data) {
        state.maxQuestionsCurrent = (data && data.max_questions) ? data.max_questions : chosenCount;
        renderQuestion(data);
      },
      error: function () { alert('Could not connect to the quiz backend.'); showScreen('start'); }
    });
  }

  // ── Wire buttons ────────────────────────────────────────────────────
  var startBtn = $('#aq-btn-start');
  if (startBtn) startBtn.onclick = function () { pickerMode = 'quiz'; loadCoursePicker(); };

  var retryBtn = $('#aq-btn-retry');
  if (retryBtn) retryBtn.onclick = function () { pickerMode = 'quiz'; loadCoursePicker(); };

  var progressBtn = $('#aq-btn-progress');
  if (progressBtn) progressBtn.onclick = function () { loadDashboard('results', selectedCourseId, selectedCourseName); };

  var backResultsBtn = $('#aq-btn-back-results');
  if (backResultsBtn) backResultsBtn.onclick = function () {
    showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
  };

  var dashRetryBtn = $('#aq-btn-dashboard-retry');
  if (dashRetryBtn) dashRetryBtn.onclick = function () { pickerMode = 'quiz'; loadCoursePicker(); };

  var progressStartBtn = $('#aq-btn-progress-start');
  if (progressStartBtn) progressStartBtn.onclick = function () { pickerMode = 'progress'; loadCoursePicker(); };

  var courseBackBtn = $('#aq-btn-course-back');
  if (courseBackBtn) courseBackBtn.onclick = function () { showScreen('start'); };

  var courseContinueBtn = $('#aq-btn-course-continue');
  if (courseContinueBtn) {
    courseContinueBtn.onclick = function () {
      var checked = element.querySelector('#aq-course-list input[type=radio]:checked');
      if (!checked) { alert('Please select a course.'); return; }
      selectedCourseId = checked.value;
      selectedCourseName = checked.getAttribute('data-course-name') || checked.value;
      if (pickerMode === 'progress') loadDashboard('start', selectedCourseId, selectedCourseName);
      else loadContentPicker(selectedCourseId);
    };
  }

  var contentBackBtn = $('#aq-btn-content-back');
  if (contentBackBtn) contentBackBtn.onclick = function () { showScreen('course'); };

  var contentStartBtn = $('#aq-btn-content-start');
  if (contentStartBtn) {
    contentStartBtn.onclick = function () {
      var checked = element.querySelectorAll('#aq-content-list input[type=checkbox]:checked');
      selectedContentIds = Array.from(checked).map(function (cb) { return cb.value; });
      if (selectedContentIds.length === 0) { alert('Please select at least one topic.'); return; }
      startSessionWithIds(selectedContentIds, selectedCourseId);
    };
  }

  // ── Init ────────────────────────────────────────────────────────────
  showScreen('start');
  var titleEl = element.querySelector('.aq-hero-title');
  if (titleEl) titleEl.textContent = DISPLAY_NAME;
}