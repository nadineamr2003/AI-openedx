/* ── Adaptive Quiz XBlock — quiz.js ────────────────────────────────── */

function AdaptiveQuizXBlock(runtime, element, initArgs) {
  var LONG_TIME_CONTEXT_THRESHOLD_MS = 90 * 1000;

  var MAX_Q = initArgs.max_questions || 10;
  var DISPLAY_NAME = initArgs.display_name || 'GUC StudyPath';

  var urlStart = runtime.handlerUrl(element, 'start_session');
  var urlSubmit = runtime.handlerUrl(element, 'submit_answer');
  var urlExplain = runtime.handlerUrl(element, 'explain_simpler');
  var urlSimilar = runtime.handlerUrl(element, 'similar_question');
  var urlProgress = runtime.handlerUrl(element, 'get_progress');
  var urlGetContent = runtime.handlerUrl(element, 'get_content');
  var urlGetCourses = runtime.handlerUrl(element, 'get_courses');
  var urlSessionHistory = runtime.handlerUrl(element, 'get_session_history');
  var urlGetDiagQ = runtime.handlerUrl(element, 'get_diagnostic_question');
  var urlSubmitDiagA = runtime.handlerUrl(element, 'submit_diagnostic_answer');
  var urlCompleteDiag = runtime.handlerUrl(element, 'complete_diagnostic_item');
  var urlFinalizeSession = runtime.handlerUrl(element, 'finalize_session');

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
    historyOrigin: 'dashboard',
    historySessions: [],
    historyPage: 0,
    historyPageSize: 3,
    explainSimplerPending: false,
    pendingAnswerKey: null,
    pendingTimeSpentMs: null,
  };

  var reviewState = {
    session: null,
    questionIndex: 0
  };

  var diagState = {
    items: [],    // [{content_id, title, topics, source_version, ...}]
    totalItems: 0,
    itemIndex: 0,
    questionIndex: 0,
    totalQuestions: 3,
    answered: false,
    questionStart: null,
    summaryPerItem: {},    // content_id → {title, correct, total, baseline, label, topicMasteries}
  };

  function getDiagnosticTargetForItem(item) {
    return parseInt((item && item.diagnostic_target_questions) || 0, 10) || 0;
  }

  function getDiagnosticTotalPlannedQuestions() {
    return diagState.items.reduce(function (sum, item) {
      return sum + getDiagnosticTargetForItem(item);
    }, 0);
  }

  function getDiagnosticAnsweredBeforeCurrent() {
    var sum = 0;
    for (var i = 0; i < diagState.itemIndex; i++) {
      sum += getDiagnosticTargetForItem(diagState.items[i]);
    }
    return sum;
  }

  function $(sel) { return element.querySelector(sel); }

  var SCREENS = ['start', 'loading', 'question', 'results', 'dashboard', 'history', 'course', 'content', 'mode', 'diagnostic', 'diagnostic-results'];

  var COURSE_PICKER_COPY = {
    quiz: {
      step: 'Step 1 of 3',
      title: 'Choose a course',
      subtitle: 'Select the course you want to practise.'
    },
    progress: {
      step: '',
      title: 'Choose a course',
      subtitle: 'Select the course whose progress you want to view.'
    },
    history: {
      step: '',
      title: 'Choose a course',
      subtitle: 'Select the course whose session history you want to view.'
    }
  };

  function updateHomeButtonVisibility(screenName) {
    var visibilityByButton = {
      '#aq-btn-results-home': screenName === 'results',
      '#aq-btn-course-home': screenName === 'course',
      '#aq-btn-content-home': screenName === 'content',
      '#aq-btn-mode-home': screenName === 'mode',
      '#aq-btn-dashboard-home': screenName === 'dashboard',
      '#aq-btn-history-home': screenName === 'history'
    };

    Object.keys(visibilityByButton).forEach(function (sel) {
      var btn = $(sel);
      if (btn) btn.classList.toggle('aq-hidden', !visibilityByButton[sel]);
    });
  }

  function showScreen(name) {
    SCREENS.forEach(function (s) {
      var el = element.querySelector('#aq-screen-' + s);
      if (el) el.classList.toggle('aq-hidden', s !== name);
    });
    updateHomeButtonVisibility(name);
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
  var selectedMode = 'normal_practice';

  function configureCoursePickerForMode() {
    var modeConfig = COURSE_PICKER_COPY[pickerMode] || COURSE_PICKER_COPY.quiz;
    var stepBadge = $('#aq-course-step-badge');
    var titleEl = $('#aq-course-title');
    var subtitleEl = $('#aq-course-subtitle');

    if (stepBadge) {
      stepBadge.textContent = modeConfig.step;
      stepBadge.classList.toggle('aq-hidden', !modeConfig.step);
    }
    if (titleEl) titleEl.textContent = modeConfig.title;
    if (subtitleEl) subtitleEl.textContent = modeConfig.subtitle;
  }

  function goHome() {
    pickerMode = 'quiz';
    showScreen('start');
  }

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
    configureCoursePickerForMode();
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
        '<input type="checkbox" value="' + item.id + '" data-title="' + item.title + '">' +
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

  function setSelectedMode(mode) {
    selectedMode = mode || 'normal_practice';

    element.querySelectorAll('.aq-mode-card').forEach(function (card) {
      card.classList.toggle('aq-mode-card-selected', card.getAttribute('data-mode') === selectedMode);
    });
  }

  function initModePicker() {
    var cards = element.querySelectorAll('.aq-mode-card');
    cards.forEach(function (card) {
      card.addEventListener('click', function () {
        setSelectedMode(card.getAttribute('data-mode'));
      });
    });

    setSelectedMode('normal_practice');
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
    hideTimeContextPrompt();

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
    if (timeSpentMs > LONG_TIME_CONTEXT_THRESHOLD_MS) {
      state.pendingAnswerKey = selectedKey;
      state.pendingTimeSpentMs = timeSpentMs;
      showTimeContextPrompt();
      return;
    }

    submitAnswer(selectedKey, timeSpentMs, null);
  }

  function submitAnswer(selectedKey, timeSpentMs, timeContext) {
    hideTimeContextPrompt();
    jQuery.ajax({
      type: 'POST', url: urlSubmit,
      data: JSON.stringify({
        selected_answer: selectedKey,
        time_spent_ms: timeSpentMs,
        time_context: timeContext || null
      }),
      contentType: 'application/json',
      success: function (data) { renderFeedback(data, selectedKey); },
      error: function () { alert('Network error submitting answer.'); }
    });
  }

  function showTimeContextPrompt() {
    var prompt = $('#aq-time-context');
    if (prompt) prompt.classList.remove('aq-hidden');
  }

  function hideTimeContextPrompt() {
    var prompt = $('#aq-time-context');
    if (prompt) prompt.classList.add('aq-hidden');
  }

  function handleTimeContextChoice(timeContext) {
    if (!state.pendingAnswerKey || state.pendingTimeSpentMs === null) return;

    var selectedKey = state.pendingAnswerKey;
    var timeSpentMs = state.pendingTimeSpentMs;

    state.pendingAnswerKey = null;
    state.pendingTimeSpentMs = null;

    submitAnswer(selectedKey, timeSpentMs, timeContext);
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
    hideExplainStatus();

    var bridgeWrap = $('#aq-narrative-bridge');
    var bridgeText = $('#aq-narrative-text');
    var bridgeLabel = $('#aq-narrative-label');

    if (bridgeWrap && bridgeText && bridgeLabel) {
      if (data.session_complete) {
        bridgeLabel.textContent = 'What happens next?';
        bridgeText.textContent =
          'You’ve finished this session. Open your results to see which topic to reinforce next and how your performance shaped the recommendation.';
        bridgeWrap.classList.remove('aq-hidden');
      } else if (data.narrative_bridge) {
        bridgeLabel.textContent = 'Why and what is the next step?';
        bridgeText.textContent = data.narrative_bridge;
        bridgeWrap.classList.remove('aq-hidden');
      } else {
        bridgeWrap.classList.add('aq-hidden');
      }
    }

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
          makeSupportBtn('🔄 One more question like this', handleSimilarQuestion)
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
    btn.type = 'button';
    btn.className = 'aq-btn-support';
    btn.textContent = label;
    btn.setAttribute('data-default-label', label);
    btn.onclick = function () { handler(btn); };
    return btn;
  }

  function setExplanationText(text) {
    var expEl = $('#aq-explanation');
    if (!expEl) return;
    expEl.classList.remove('aq-explanation-loading');
    expEl.textContent = text || '';
  }

  function setExplanationLoading(message) {
    var expEl = $('#aq-explanation');
    if (!expEl) return;
    expEl.classList.add('aq-explanation-loading');
    expEl.innerHTML =
      '<span class="aq-inline-spinner" aria-hidden="true"></span>' +
      '<span>' + escapeHtml(message || 'Simplifying explanation…') + '</span>';
  }

  function getExplainStatusEl() {
    var statusEl = $('#aq-explain-status');
    if (statusEl) return statusEl;

    var expEl = $('#aq-explanation');
    if (!expEl || !expEl.parentNode) return null;

    statusEl = document.createElement('div');
    statusEl.id = 'aq-explain-status';
    statusEl.className = 'aq-explain-status aq-hidden';
    expEl.parentNode.insertBefore(statusEl, expEl.nextSibling);
    return statusEl;
  }

  function showExplainStatus(message) {
    var statusEl = getExplainStatusEl();
    if (!statusEl) return;
    statusEl.textContent = message || '';
    statusEl.classList.remove('aq-hidden');
  }

  function hideExplainStatus() {
    var statusEl = getExplainStatusEl();
    if (!statusEl) return;
    statusEl.textContent = '';
    statusEl.classList.add('aq-hidden');
  }

  function setExplainSimplerButtonState(btn, isLoading) {
    if (!btn) return;
    var defaultLabel = btn.getAttribute('data-default-label') || '💬 Simpler explanation';
    btn.disabled = !!isLoading;
    btn.classList.toggle('aq-btn-support-disabled', !!isLoading);
    btn.textContent = isLoading ? '💬 Simplifying…' : defaultLabel;
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

  function masteryPct(value) {
    var numeric = Number(value);
    if (!isFinite(numeric)) numeric = 0;
    return Math.round(numeric * 100);
  }

  function masteryDeltaFromDisplayed(beforePct, afterPct) {
    return afterPct - beforePct;
  }

  function formatMasteryDelta(deltaPct) {
    if (deltaPct > 0) return '+' + deltaPct + '%';
    if (deltaPct < 0) return deltaPct + '%';
    return '0%';
  }

  function formatMs(ms) {
    if (!ms || ms <= 0) return '—';
    var seconds = Math.round(ms / 1000);
    if (seconds < 60) return seconds + 's';
    var mins = Math.floor(seconds / 60);
    var rem = seconds % 60;
    return mins + 'm ' + rem + 's';
  }

  function formatModeLabel(mode) {
    var m = String(mode || '').toLowerCase();
    if (m === 'normal_practice') return 'Normal Practice';
    if (m === 'weakness_review') return 'Weakness Review';
    if (m === 'challenge') return 'Challenge';
    if (m === 'auto') return 'Auto';
    return mode || '—';
  }

  function isFollowUpSession(sessionLike) {
    return String((sessionLike && sessionLike.session_origin) || '').toLowerCase() === 'followup';
  }

  function getPracticedTopics(sessionLike) {
    return normalizeTopicList(
      Array.isArray(sessionLike && sessionLike.practiced_topics)
        ? sessionLike.practiced_topics
        : []
    );
  }

  function getRecommendedReviewTopics(sessionLike) {
    var recommendedTopics = normalizeTopicList(
      Array.isArray(sessionLike && sessionLike.recommended_review_topics)
        ? sessionLike.recommended_review_topics
        : []
    );

    if (recommendedTopics.length) return recommendedTopics.slice(0, 2);

    var fallbackTopic = String((sessionLike && sessionLike.recommended_review_topic) || '').trim();
    return fallbackTopic ? [fallbackTopic] : [];
  }

  function getFollowUpTopicMasterySummaries(sessionLike) {
    var summaries = Array.isArray(sessionLike && sessionLike.followup_topic_mastery_summaries)
      ? sessionLike.followup_topic_mastery_summaries
      : [];
    if (summaries.length) return summaries;

    var focusedSummary = sessionLike && sessionLike.focused_topic_mastery_summary;
    if (focusedSummary && String(focusedSummary.topic || '').trim()) {
      return [focusedSummary];
    }

    return [];
  }

  function getFollowUpPracticedTopics(sessionLike) {
    var explicitTopics = normalizeTopicList(
      Array.isArray(sessionLike && sessionLike.followup_topics_practised)
        ? sessionLike.followup_topics_practised
        : []
    );
    if (explicitTopics.length) return explicitTopics.slice(0, 2);

    var summaryTopics = normalizeTopicList(
      getFollowUpTopicMasterySummaries(sessionLike).map(function (summary) {
        return summary && summary.topic;
      })
    );
    if (summaryTopics.length) return summaryTopics.slice(0, 2);

    return getPracticedTopics(sessionLike).slice(0, 2);
  }

  function isSingleTopicFollowUpSession(sessionLike) {
    if (!isFollowUpSession(sessionLike)) return false;

    var focusedSummary = sessionLike && sessionLike.focused_topic_mastery_summary;
    if (focusedSummary && String(focusedSummary.topic || '').trim()) return true;

    var topicsPractised = parseInt((sessionLike && sessionLike.topics_practised_count) || 0, 10);
    if (topicsPractised === 1) return true;

    return getPracticedTopics(sessionLike).length === 1;
  }

  function isMultiTopicFollowUpSession(sessionLike) {
    return isFollowUpSession(sessionLike) && getFollowUpPracticedTopics(sessionLike).length > 1;
  }

  function formatTopicList(topics) {
    var normalized = normalizeTopicList(topics);
    if (!normalized.length) return '';
    if (normalized.length === 1) return normalized[0];
    if (normalized.length === 2) return normalized[0] + ' and ' + normalized[1];
    return normalized.slice(0, -1).join(', ') + ', and ' + normalized[normalized.length - 1];
  }

  function getFocusedTopicName(sessionLike) {
    var focusedSummary = sessionLike && sessionLike.focused_topic_mastery_summary;
    if (focusedSummary && String(focusedSummary.topic || '').trim()) {
      return String(focusedSummary.topic).trim();
    }

    var practicedTopics = getPracticedTopics(sessionLike);
    if (practicedTopics.length === 1) return practicedTopics[0];

    var recommendedTopics = getRecommendedReviewTopics(sessionLike);
    if (recommendedTopics.length === 1) return recommendedTopics[0];

    var recommendationTopic = String((sessionLike && sessionLike.recommended_review_topic) || '').trim();
    if (recommendationTopic) return recommendationTopic;

    var strongestTopic = String((sessionLike && sessionLike.strongest_topic_this_session) || '').trim();
    if (strongestTopic) return strongestTopic;

    var weakestTopic = String((sessionLike && sessionLike.weakest_topic_this_session) || '').trim();
    return weakestTopic;
  }

  function getLearnerSessionLabel(sessionLike) {
    if (isFollowUpSession(sessionLike)) return 'Follow-up Quiz';
    return formatModeLabel(sessionLike && sessionLike.selected_mode);
  }

  function normalizeTopicList(topics) {
    if (!Array.isArray(topics)) return [];

    return topics
      .map(function (topic) { return String(topic || '').trim(); })
      .filter(function (topic, index, arr) {
        return topic && arr.indexOf(topic) === index;
      });
  }

  function getFollowUpContext(sessionLike) {
    if (isFollowUpSession(sessionLike)) {
      return null;
    }

    var topics = getRecommendedReviewTopics(sessionLike);
    var courseId = String((sessionLike && sessionLike.course_id) || '').trim();
    var contentIds = Array.isArray(sessionLike && sessionLike.selected_content_ids)
      ? sessionLike.selected_content_ids.filter(function (contentId) { return !!contentId; })
      : [];
    var questionCount = parseInt((sessionLike && sessionLike.target_questions) || state.maxQuestionsCurrent || MAX_Q, 10) || MAX_Q;

    if (!topics.length || !courseId || contentIds.length === 0) {
      return null;
    }

    return {
      topics: topics,
      topicText: formatTopicList(topics),
      courseId: courseId,
      contentIds: contentIds,
      questionCount: questionCount
    };
  }

  function startFocusedFollowUp(context) {
    if (!context) return;

    selectedCourseId = context.courseId;
    selectedContentIds = context.contentIds.slice();
    selectedMode = 'weakness_review';

    startSessionWithIds(context.contentIds, context.courseId, 'weakness_review', {
      focusTopics: context.topics,
      questionCount: context.questionCount,
      sessionOrigin: 'followup'
    });
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

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function contentTypeLabel(type) {
    var t = String(type || '').toLowerCase();
    if (t === 'lecture') return 'Lectures';
    if (t === 'tutorial') return 'Tutorials';
    if (t === 'lab') return 'Labs';
    return t ? t.charAt(0).toUpperCase() + t.slice(1) + 's' : 'Other Content';
  }

  function contentTypeOrder(type) {
    var t = String(type || '').toLowerCase();
    if (t === 'lecture') return 1;
    if (t === 'tutorial') return 2;
    if (t === 'lab') return 3;
    return 99;
  }

  function renderGroupedMastery(topicsWrap, mastery, topicLabels, contentItems) {
    topicsWrap.innerHTML = '';

    if (!contentItems || contentItems.length === 0) {
      renderFlatMastery(topicsWrap, mastery, topicLabels);
      return;
    }

    var grouped = {};

    contentItems.forEach(function (item) {
      var type = (item.content_type || 'other').toLowerCase();
      var itemTopics = (item.topics || []).filter(function (topic) {
        return Object.prototype.hasOwnProperty.call(mastery, topic);
      });

      if (itemTopics.length === 0) return;

      if (!grouped[type]) grouped[type] = [];

      var topicEntries = itemTopics.map(function (topic) {
        var pct = Math.round((mastery[topic] || 0) * 100);
        var label = topicLabels[topic] || 'Developing';
        var cls = masteryStageClass(label);
        return {
          topic: topic,
          pct: pct,
          label: label,
          cls: cls
        };
      });

      var avgPct = Math.round(
        topicEntries.reduce(function (sum, t) { return sum + t.pct; }, 0) / topicEntries.length
      );

      grouped[type].push({
        title: item.title || 'Untitled Content',
        week: item.week,
        topics: topicEntries,
        avgPct: avgPct
      });
    });

    var typeKeys = Object.keys(grouped).sort(function (a, b) {
      return contentTypeOrder(a) - contentTypeOrder(b);
    });

    if (typeKeys.length === 0) {
      renderFlatMastery(topicsWrap, mastery, topicLabels);
      return;
    }

    typeKeys.forEach(function (type, typeIndex) {
      var items = grouped[type].sort(function (a, b) {
        var aw = typeof a.week === 'number' ? a.week : 999;
        var bw = typeof b.week === 'number' ? b.week : 999;
        if (aw !== bw) return aw - bw;
        return String(a.title).localeCompare(String(b.title));
      });

      var totalTopics = items.reduce(function (sum, item) {
        return sum + item.topics.length;
      }, 0);

      var weightedSum = items.reduce(function (sum, item) {
        return sum + (item.avgPct * item.topics.length);
      }, 0);

      var avgPct = totalTopics > 0 ? Math.round(weightedSum / totalTopics) : 0;

      var typeDetails = document.createElement('details');
      typeDetails.className = 'aq-accordion aq-accordion-type';
      if (typeIndex === 0) typeDetails.open = true;

      typeDetails.innerHTML =
        '<summary class="aq-accordion-summary">' +
        '<div class="aq-accordion-summary-main">' +
        '<span class="aq-accordion-title">' + escapeHtml(contentTypeLabel(type)) + '</span>' +
        '<span class="aq-accordion-meta">' + items.length + ' item' + (items.length === 1 ? '' : 's') + ' · ' + totalTopics + ' topic' + (totalTopics === 1 ? '' : 's') + '</span>' +
        '</div>' +
        '<div class="aq-accordion-summary-side">' +
        '<span class="aq-accordion-score">' + avgPct + '% avg</span>' +
        '<span class="aq-accordion-chevron">⌄</span>' +
        '</div>' +
        '</summary>';

      var typeBody = document.createElement('div');
      typeBody.className = 'aq-accordion-body';

      items.forEach(function (item, itemIndex) {
        var itemDetails = document.createElement('details');
        itemDetails.className = 'aq-accordion aq-accordion-item';
        if (typeIndex === 0 && itemIndex === 0) itemDetails.open = true;

        var weekText = (typeof item.week === 'number') ? ('Week ' + item.week + ' · ') : '';

        itemDetails.innerHTML =
          '<summary class="aq-accordion-summary aq-accordion-summary-item">' +
          '<div class="aq-accordion-summary-main">' +
          '<span class="aq-accordion-title aq-accordion-title-item">' + escapeHtml(item.title) + '</span>' +
          '<span class="aq-accordion-meta">' + weekText + item.topics.length + ' topic' + (item.topics.length === 1 ? '' : 's') + '</span>' +
          '</div>' +
          '<div class="aq-accordion-summary-side">' +
          '<span class="aq-accordion-score">' + item.avgPct + '% avg</span>' +
          '<span class="aq-accordion-chevron">⌄</span>' +
          '</div>' +
          '</summary>';

        var itemBody = document.createElement('div');
        itemBody.className = 'aq-accordion-body aq-accordion-body-item';

        item.topics.forEach(function (entry) {
          var topicBlock = document.createElement('div');
          topicBlock.className = 'aq-dash-topic';
          topicBlock.innerHTML =
            '<div class="aq-dash-topic-row">' +
            '<span class="aq-dash-topic-name">' + escapeHtml(entry.topic) + '</span>' +
            '<span class="aq-dash-topic-badge ' + entry.cls + '">' + entry.pct + '% · ' + escapeHtml(entry.label) + '</span>' +
            '</div>' +
            '<div class="aq-dash-track">' +
            '<div class="aq-dash-fill ' + entry.cls + '" style="width:' + entry.pct + '%"></div>' +
            '</div>';

          itemBody.appendChild(topicBlock);
        });

        itemDetails.appendChild(itemBody);
        typeBody.appendChild(itemDetails);
      });

      typeDetails.appendChild(typeBody);
      topicsWrap.appendChild(typeDetails);
    });
  }

  function renderFlatMastery(topicsWrap, mastery, topicLabels) {
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
        '<span class="aq-dash-topic-name">' + escapeHtml(topic) + '</span>' +
        '<span class="aq-dash-topic-badge ' + cls + '">' + badgeText + '</span>' +
        '</div>' +
        '<div class="aq-dash-track">' +
        '<div class="aq-dash-fill ' + cls + '" style="width:' + pct + '%"></div>' +
        '</div>';

      topicsWrap.appendChild(block);
    });
  }

  function handleExplainSimpler(btn) {
    if (state.explainSimplerPending) return;

    var expEl = $('#aq-explanation');
    var originalExplanation = expEl ? expEl.textContent : '';

    state.explainSimplerPending = true;
    hideExplainStatus();
    setExplainSimplerButtonState(btn, true);
    setExplanationLoading('Simplifying explanation...');

    jQuery.ajax({
      type: 'POST', url: urlExplain,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        state.explainSimplerPending = false;
        setExplainSimplerButtonState(btn, false);

        if (data && data.success) {
          setExplanationText(data.simpler_explanation);
          hideExplainStatus();
          return;
        }
        setExplanationText(originalExplanation);
        showExplainStatus('Could not simplify the explanation just now.');
      },
      error: function () {
        state.explainSimplerPending = false;
        setExplainSimplerButtonState(btn, false);
        setExplanationText(originalExplanation);
        showExplainStatus('Could not simplify the explanation just now.');
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

  function classifyFollowUpOutcome(summary, sessionAccuracy) {
    var masteryDelta = summary && typeof summary.mastery_delta === 'number'
      ? summary.mastery_delta
      : 0;
    var accuracy = typeof sessionAccuracy === 'number' ? sessionAccuracy : 0;

    if (masteryDelta >= 0.03 && accuracy >= 0.8) {
      return 'Strongly Reinforced';
    }
    if (masteryDelta > 0) {
      return 'Improved';
    }
    if (masteryDelta === 0 && accuracy >= 0.6) {
      return 'Stabilising';
    }
    return 'Still Needs Review';
  }

  function classifyMultiTopicFollowUpOutcome(summaries, sessionAccuracy) {
    var followUpSummaries = Array.isArray(summaries) ? summaries : [];
    var accuracy = typeof sessionAccuracy === 'number' ? sessionAccuracy : 0;
    var positiveCount = followUpSummaries.filter(function (summary) {
      return summary && typeof summary.mastery_delta === 'number' && summary.mastery_delta > 0;
    }).length;
    var strongCount = followUpSummaries.filter(function (summary) {
      return summary && typeof summary.mastery_delta === 'number' && summary.mastery_delta >= 0.03;
    }).length;

    if (followUpSummaries.length && strongCount === followUpSummaries.length && accuracy >= 0.8) {
      return 'Strong Reinforcement';
    }
    if (followUpSummaries.length && positiveCount === followUpSummaries.length && accuracy >= 0.6) {
      return 'Consistent Improvement';
    }
    if (positiveCount > 0 || accuracy >= 0.6) {
      return 'Mixed Improvement';
    }
    return 'Still Needs Review';
  }

  function renderSessionInsight(data) {
    var wrap = $('#aq-session-insight');
    if (!wrap) return;

    var focusedFollowUp = isSingleTopicFollowUpSession(data);
    var multiTopicFollowUp = isMultiTopicFollowUpSession(data);
    var followUpTopics = getFollowUpPracticedTopics(data);
    var followUpSummaries = getFollowUpTopicMasterySummaries(data);
    var strongest = data.strongest_topic_this_session || '';
    var weakest = data.weakest_topic_this_session || '';
    var recommendation = data.session_recommendation || '';
    var avgTime = data.avg_time_spent_ms || 0;
    var sectionTitleEl = $('#aq-results-insight-title');
    var gridEl = $('#aq-insight-grid');
    var strongestCardEl = $('#aq-insight-card-strongest');
    var weakestCardEl = $('#aq-insight-card-weakest');
    var avgTimeCardEl = $('#aq-insight-card-avg-time');
    var strongestLabelEl = $('#aq-insight-label-strongest');
    var weakestLabelEl = $('#aq-insight-label-weakest');
    var avgTimeLabelEl = $('#aq-insight-label-avg-time');
    var recommendationCardEl = $('#aq-recommendation-card');

    if (focusedFollowUp) {
      strongest = classifyFollowUpOutcome(
        data && data.focused_topic_mastery_summary,
        data && data.session_accuracy
      );
    } else if (multiTopicFollowUp) {
      strongest = (followUpTopics.length || parseInt(data.topics_practised_count || 0, 10) || 0) + ' topics reviewed';
      weakest = classifyMultiTopicFollowUpOutcome(
        followUpSummaries,
        data && data.session_accuracy
      );
    }

    if (!strongest && !weakest && !recommendation) {
      wrap.classList.add('aq-hidden');
      return;
    }

    var strongestEl = $('#aq-insight-strongest');
    var weakestEl = $('#aq-insight-weakest');
    var avgTimeEl = $('#aq-insight-avg-time');
    var recTextEl = $('#aq-recommendation-text');

    if (sectionTitleEl) sectionTitleEl.textContent = (focusedFollowUp || multiTopicFollowUp) ? 'Follow-up Insight' : 'Session Insight';
    if (gridEl) gridEl.classList.toggle('aq-insight-grid-single', focusedFollowUp);
    if (strongestCardEl) strongestCardEl.classList.remove('aq-hidden');
    if (weakestCardEl) weakestCardEl.classList.toggle('aq-hidden', focusedFollowUp);
    if (avgTimeCardEl) avgTimeCardEl.classList.toggle('aq-hidden', focusedFollowUp || multiTopicFollowUp);

    if (strongestLabelEl) {
      strongestLabelEl.textContent = focusedFollowUp
        ? 'Follow-up Outcome'
        : multiTopicFollowUp
          ? 'Follow-up Coverage'
          : 'Best Performed Topic';
    }
    if (weakestLabelEl) weakestLabelEl.textContent = multiTopicFollowUp ? 'Outcome' : 'Needs Review';
    if (avgTimeLabelEl) avgTimeLabelEl.textContent = 'Avg Response Time';

    if (strongestEl) strongestEl.textContent = strongest || '—';
    if (weakestEl) weakestEl.textContent = focusedFollowUp ? '—' : (weakest || '—');
    if (avgTimeEl) avgTimeEl.textContent = (focusedFollowUp || multiTopicFollowUp) ? '—' : formatMs(avgTime);

    if (recommendationCardEl) {
      recommendationCardEl.classList.toggle('aq-hidden', (focusedFollowUp || multiTopicFollowUp) && !recommendation);
    }
    if (recTextEl) {
      recTextEl.textContent = (focusedFollowUp || multiTopicFollowUp)
        ? (recommendation || '')
        : (recommendation || 'Keep practising to reinforce your learning.');
    }

    wrap.classList.remove('aq-hidden');
  }

  function renderLectureMasterySummary(data) {
    var wrap = $('#aq-results-lecture-summary');
    var listEl = $('#aq-results-lecture-list');
    var titleEl = $('#aq-results-mastery-title');
    if (!wrap || !listEl || !titleEl) return;

    listEl.innerHTML = '';
    titleEl.textContent = 'Lecture Mastery Change';

    var focusedSummary = data && data.focused_topic_mastery_summary;
    var followUpSummaries = getFollowUpTopicMasterySummaries(data);
    if (isMultiTopicFollowUpSession(data) && followUpSummaries.length > 1) {
      titleEl.textContent = 'Follow-up Topic Mastery';

      followUpSummaries.slice(0, 2).forEach(function (summary) {
        var beforePct = masteryPct(summary.mastery_before);
        var afterPct = masteryPct(summary.mastery_after);
        var deltaPct = masteryDeltaFromDisplayed(beforePct, afterPct);
        var deltaClass = deltaPct > 0 ? 'up' : deltaPct < 0 ? 'down' : 'flat';
        var deltaArrow = deltaPct > 0 ? '↑' : deltaPct < 0 ? '↓' : '→';
        var deltaLabel = formatMasteryDelta(deltaPct);
        var card = document.createElement('div');

        card.className = 'aq-lecture-change-card aq-lecture-change-card-followup';
        card.innerHTML =
          '<div class="aq-lecture-change-top">' +
          '<div>' +
          '<div class="aq-lecture-change-title">' + escapeHtml(summary.topic) + '</div>' +
          '<div class="aq-lecture-change-meta">Follow-up topic</div>' +
          '</div>' +
          '<span class="aq-lecture-change-delta ' + deltaClass + '">' + deltaArrow + ' ' + deltaLabel + '</span>' +
          '</div>' +
          '<div class="aq-lecture-change-values">' + beforePct + '% → ' + afterPct + '%</div>';

        listEl.appendChild(card);
      });

      wrap.classList.remove('aq-hidden');
      return;
    }

    if (isSingleTopicFollowUpSession(data) && focusedSummary && focusedSummary.topic) {
      var beforePct = masteryPct(focusedSummary.mastery_before);
      var afterPct = masteryPct(focusedSummary.mastery_after);
      var deltaPct = masteryDeltaFromDisplayed(beforePct, afterPct);
      var deltaClass = deltaPct > 0 ? 'up' : deltaPct < 0 ? 'down' : 'flat';
      var deltaArrow = deltaPct > 0 ? '↑' : deltaPct < 0 ? '↓' : '→';
      var deltaLabel = formatMasteryDelta(deltaPct);
      var focusedCard = document.createElement('div');

      titleEl.textContent = 'Focused Topic Mastery';
      focusedCard.className = 'aq-lecture-change-card aq-lecture-change-card-followup';
      focusedCard.innerHTML =
        '<div class="aq-lecture-change-top">' +
        '<div>' +
        '<div class="aq-lecture-change-title">' + escapeHtml(focusedSummary.topic) + '</div>' +
        '<div class="aq-lecture-change-meta">Single-topic follow-up</div>' +
        '</div>' +
        '<span class="aq-lecture-change-delta ' + deltaClass + '">' + deltaArrow + ' ' + deltaLabel + '</span>' +
        '</div>' +
        '<div class="aq-lecture-change-values">' + beforePct + '% → ' + afterPct + '%</div>';

      listEl.appendChild(focusedCard);
      wrap.classList.remove('aq-hidden');
      return;
    }

    var items = (data && data.content_mastery_summaries) || [];
    if (!items || items.length === 0) {
      wrap.classList.add('aq-hidden');
      return;
    }

    items.forEach(function (item) {
      var beforePct = masteryPct(item.avg_mastery_before);
      var afterPct = masteryPct(item.avg_mastery_after);
      var deltaPct = masteryDeltaFromDisplayed(beforePct, afterPct);
      var deltaClass = deltaPct > 0 ? 'up' : deltaPct < 0 ? 'down' : 'flat';
      var deltaArrow = deltaPct > 0 ? '↑' : deltaPct < 0 ? '↓' : '→';
      var deltaLabel = formatMasteryDelta(deltaPct);
      var topicCount = item.topic_count || 0;

      var card = document.createElement('div');
      card.className = 'aq-lecture-change-card';
      card.innerHTML =
        '<div class="aq-lecture-change-top">' +
        '<div>' +
        '<div class="aq-lecture-change-title">' + escapeHtml(item.title || 'Selected Lecture') + '</div>' +
        '<div class="aq-lecture-change-meta">' + topicCount + ' topic' + (topicCount === 1 ? '' : 's') + '</div>' +
        '</div>' +
        '<span class="aq-lecture-change-delta ' + deltaClass + '">' + deltaArrow + ' ' + deltaLabel + '</span>' +
        '</div>' +
        '<div class="aq-lecture-change-values">' + beforePct + '% → ' + afterPct + '%</div>';

      listEl.appendChild(card);
    });

    wrap.classList.remove('aq-hidden');
  }

  function renderResultsFollowUp(data) {
    var wrap = $('#aq-results-follow-up');
    var topicEl = $('#aq-results-follow-up-topic');
    var btn = $('#aq-btn-results-follow-up');
    if (!wrap || !topicEl || !btn) return;

    var followUp = getFollowUpContext(data);
    if (!followUp) {
      wrap.classList.add('aq-hidden');
      btn.onclick = null;
      return;
    }

    topicEl.textContent = 'Focused on: ' + followUp.topicText;
    btn.onclick = function () {
      startFocusedFollowUp(followUp);
    };
    wrap.classList.remove('aq-hidden');
  }

  // ── Results ────────────────────────────────────────────────────────
  function showResults(data) {
    var score = data.session_score || state.sessionScore;
    var total = state.maxQuestionsCurrent;
    var pct = Math.round((score / total) * 100);
    var sessionAccuracy = typeof data.session_accuracy === 'number'
      ? Math.round(data.session_accuracy * 100)
      : pct;
    var lecturesPractised = data.lectures_practised_count;
    var topicsPractised = data.topics_practised_count;
    var lectureSummaries = data.content_mastery_summaries || [];

    if (typeof lecturesPractised !== 'number') {
      lecturesPractised = lectureSummaries.length;
    }

    if (typeof topicsPractised !== 'number') {
      topicsPractised = Array.isArray(data.practiced_topics) ? data.practiced_topics.length : 0;
    }

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
      '#aq-summary-accuracy': sessionAccuracy + '%',
      '#aq-summary-avg-time': formatMs(data.avg_time_spent_ms),
      '#aq-summary-lectures': String(lecturesPractised || 0),
      '#aq-summary-topics': String(topicsPractised || 0)
    };
    Object.keys(els).forEach(function (sel) {
      var el = element.querySelector(sel);
      if (el) el.textContent = els[sel];
    });

    var pb = $('#aq-progress-bar');
    if (pb) pb.style.width = '100%';

    renderSessionInsight(data);
    renderLectureMasterySummary(data);
    renderResultsFollowUp(data);

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

    var overallAccuracy = '—';
    if (typeof data.overall_accuracy === 'number') {
      overallAccuracy = Math.round(data.overall_accuracy * 100) + '%';
    }

    var fields = {
      '#aq-dash-sessions': data.session_count || 0,
      '#aq-dash-total-answers': data.total_answers || 0,
      '#aq-dash-overall-accuracy': overallAccuracy,
      '#aq-dash-avg-time': formatMs(data.overall_avg_time_spent_ms || 0)
    };
    Object.keys(fields).forEach(function (sel) {
      var el = element.querySelector(sel);
      if (el) el.textContent = fields[sel];
    });

    if (topicsWrap) {
      var mastery = data.topic_mastery || {};
      var topicLabels = data.topic_labels || {};
      var contentItems = data.content_items || [];

      renderGroupedMastery(topicsWrap, mastery, topicLabels, contentItems);
    }

    showScreen('dashboard');
  }

  function renderSessionHistory(sessions, options) {
    options = options || {};
    var wrap = options.wrap || $('#aq-session-history-list');
    var empty = options.empty || $('#aq-session-history-empty');
    var showReviewButton = !!options.showReviewButton;
    var allowFollowUp = !!options.allowFollowUp;

    if (!wrap) return;
    wrap.innerHTML = '';

    if (!sessions || sessions.length === 0) {
      if (empty) empty.classList.remove('aq-hidden');
      return;
    }

    if (empty) empty.classList.add('aq-hidden');

    sessions.forEach(function (session, idx) {
      var score = (session.correct_answers || 0) + ' / ' + (session.target_questions || 0);
      var pct = Math.round((session.accuracy || 0) * 100);
      var modeText = getLearnerSessionLabel(session);
      var lectureTitles = Array.isArray(session.selected_content_titles) && session.selected_content_titles.length
        ? session.selected_content_titles.join(', ')
        : '—';
      var focusedFollowUp = isSingleTopicFollowUpSession(session);
      var multiTopicFollowUp = isMultiTopicFollowUpSession(session);
      var isFollowUp = isFollowUpSession(session);
      var followUpTopics = getFollowUpPracticedTopics(session);
      var primaryLabel = focusedFollowUp ? 'Focus Topic' : (multiTopicFollowUp ? 'Topics Reviewed' : 'Best Performed Topic');
      var primaryValue = focusedFollowUp
        ? (getFocusedTopicName(session) || '—')
        : (multiTopicFollowUp
          ? (formatTopicList(followUpTopics) || ((followUpTopics.length || 2) + ' topics reviewed'))
          : (session.strongest_topic_this_session || '—'));
      var secondaryLabel = focusedFollowUp ? 'Accuracy' : (multiTopicFollowUp ? 'Outcome' : 'Needs Review');
      var secondaryValue = focusedFollowUp
        ? (pct + '%')
        : (multiTopicFollowUp
          ? classifyMultiTopicFollowUpOutcome(getFollowUpTopicMasterySummaries(session), session.accuracy)
          : (session.weakest_topic_this_session || '—'));
      var followUp = allowFollowUp ? getFollowUpContext(session) : null;

      var actionsHtml = '';
      var actionButtons = [];
      if (showReviewButton && session.question_log && session.question_log.length) {
        actionButtons.push(
          '<button class="aq-btn-session aq-btn-session-review" type="button" data-session-index="' + idx + '">Review Session</button>'
        );
      }
      if (followUp) {
        actionButtons.push(
          '<button class="aq-btn-session aq-btn-session-follow-up" type="button" data-session-index="' + idx + '">Start Follow-up Quiz</button>'
        );
      }
      if (actionButtons.length) {
        actionsHtml =
          '<div class="aq-session-actions-wrap">' +
          (followUp
            ? '<div class="aq-follow-up-sub aq-follow-up-sub-card">Focused on: ' + escapeHtml(followUp.topicText) + '</div>'
            : '') +
          '<div class="aq-session-actions">' +
          actionButtons.join('') +
          '</div>' +
          '</div>';
      }

      var card = document.createElement('div');
      card.className = 'aq-session-card' + (isFollowUp ? ' aq-session-card-followup' : '');
      card.innerHTML =
        '<div class="aq-session-card-top">' +
        '<div>' +
        '<div class="aq-session-date">' + formatDateTime(session.ended_at || session.started_at) + '</div>' +
        '<div class="aq-session-meta-row">' +
        '<div class="aq-session-meta">' + (isFollowUp ? 'Completed follow-up quiz' : 'Completed session') + '</div>' +
        (isFollowUp ? '<span class="aq-session-kind-badge aq-session-kind-badge-followup">Follow-up Quiz</span>' : '') +
        '</div>' +
        '</div>' +
        '<div class="aq-session-score-badge">' + score + ' · ' + pct + '%</div>' +
        '</div>' +

        '<div class="aq-session-context">' +
        '<div class="aq-session-context-row">' +
        '<span class="aq-session-context-label">Mode</span>' +
        '<span class="aq-session-context-value">' + escapeHtml(modeText) + '</span>' +
        '</div>' +
        '<div class="aq-session-context-row">' +
        '<span class="aq-session-context-label">Lectures</span>' +
        '<span class="aq-session-context-value">' + escapeHtml(lectureTitles) + '</span>' +
        '</div>' +
        '</div>' +

        '<div class="aq-session-card-grid">' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">' + primaryLabel + '</span>' +
        '<span class="aq-session-mini-value">' + escapeHtml(primaryValue) + '</span>' +
        '</div>' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">' + secondaryLabel + '</span>' +
        '<span class="aq-session-mini-value">' + escapeHtml(secondaryValue) + '</span>' +
        '</div>' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">Avg Response Time</span>' +
        '<span class="aq-session-mini-value">' + formatMs(session.avg_time_spent_ms || 0) + '</span>' +
        '</div>' +
        '</div>' +

        '<div class="aq-session-recommendation">' +
        '<strong>Recommendation:</strong> ' + escapeHtml(session.recommendation || 'Keep building mastery through regular practice.') +
        '</div>' +

        actionsHtml;

      wrap.appendChild(card);
    });

    if (showReviewButton) {
      wrap.querySelectorAll('.aq-btn-session-review').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var idx = parseInt(btn.getAttribute('data-session-index'), 10);
          openSessionReview(sessions[idx]);
        });
      });
    }

    if (allowFollowUp) {
      wrap.querySelectorAll('.aq-btn-session-follow-up').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var idx = parseInt(btn.getAttribute('data-session-index'), 10);
          var followUp = getFollowUpContext(sessions[idx]);
          if (followUp) startFocusedFollowUp(followUp);
        });
      });
    }
  }

  function updateHistoryPager() {
    var total = state.historySessions.length;
    var pageSize = state.historyPageSize;
    var totalPages = total > 0 ? Math.ceil(total / pageSize) : 1;
    var currentPage = state.historyPage + 1;
    var pagerConfigs = [
      {
        wrap: $('#aq-history-pager-wrap'),
        info: $('#aq-history-page-info'),
        prev: $('#aq-btn-history-prev'),
        next: $('#aq-btn-history-next')
      },
      {
        wrap: $('#aq-history-pager-wrap-bottom'),
        info: $('#aq-history-page-info-bottom'),
        prev: $('#aq-btn-history-prev-bottom'),
        next: $('#aq-btn-history-next-bottom')
      }
    ];

    pagerConfigs.forEach(function (pager) {
      if (pager.wrap) {
        pager.wrap.classList.toggle('aq-hidden', total === 0);
      }
      if (pager.info) {
        pager.info.textContent = 'Page ' + currentPage + ' of ' + totalPages;
      }
      if (pager.prev) pager.prev.disabled = state.historyPage <= 0;
      if (pager.next) pager.next.disabled = state.historyPage >= totalPages - 1;
    });
  }

  function renderHistoryPage() {
    var start = state.historyPage * state.historyPageSize;
    var end = start + state.historyPageSize;
    var pageSessions = state.historySessions.slice(start, end);

    renderSessionHistory(pageSessions, {
      wrap: $('#aq-history-list'),
      empty: $('#aq-history-empty'),
      showReviewButton: true,
      allowFollowUp: true
    });

    updateHistoryPager();
  }

  function answerDisplay(question, key) {
    if (!key) return '—';
    var options = question.options || {};
    var text = options[key] || '';
    return text ? (key + ' — ' + text) : key;
  }

  function renderReviewQuestion() {
    var session = reviewState.session;
    if (!session || !session.question_log || !session.question_log.length) return;

    var q = session.question_log[reviewState.questionIndex];
    var total = session.question_log.length;

    $('#aq-review-session-title').textContent = 'Session Review';
    $('#aq-review-session-sub').textContent =
      formatDateTime(session.ended_at || session.started_at) +
      ' · Score: ' + (session.correct_answers || 0) + '/' + (session.target_questions || 0);

    $('#aq-review-topic').textContent = q.topic || 'General';
    $('#aq-review-counter').textContent = (reviewState.questionIndex + 1) + ' / ' + total;

    var diff = q.difficulty || 3;
    var diffEl = $('#aq-review-difficulty');
    diffEl.textContent = DIFF_LABEL[diff] || 'Medium';
    diffEl.className = 'aq-tag aq-tag-diff ' + (DIFF_CLASS[diff] || '');

    $('#aq-review-question-text').textContent = q.question_text || q.question_id || 'Question unavailable';

    var optionsWrap = $('#aq-review-options');
    optionsWrap.innerHTML = '';

    ['A', 'B', 'C', 'D'].forEach(function (key) {
      if (!q.options || !q.options[key]) return;

      var option = document.createElement('button');
      option.type = 'button';
      option.disabled = true;
      option.className = 'aq-opt';

      if (key === q.selected_answer && key === q.correct_answer) {
        option.classList.add('selected', 'correct');
      } else if (key === q.selected_answer && key !== q.correct_answer) {
        option.classList.add('selected', 'incorrect');
      } else if (key === q.correct_answer) {
        option.classList.add('correct');
      }

      option.innerHTML =
        '<span class="aq-opt-key">' + key + '</span>' +
        '<span class="aq-opt-text">' + escapeHtml(q.options[key]) + '</span>';

      optionsWrap.appendChild(option);
    });

    var banner = $('#aq-review-banner');
    var icon = $('#aq-review-icon');
    var label = $('#aq-review-label');

    if (q.is_correct) {
      banner.className = 'aq-feedback-banner correct';
      icon.textContent = '✓';
      label.textContent = 'Correct';
    } else {
      banner.className = 'aq-feedback-banner incorrect';
      icon.textContent = '✕';
      label.textContent = 'Incorrect';
    }

    $('#aq-review-time-chip').textContent = 'Time: ' + formatMs(q.time_spent_ms || 0);
    $('#aq-review-explanation').textContent = q.explanation || 'No explanation stored for this question.';

    var prevBtn = $('#aq-btn-review-prev');
    var nextBtn = $('#aq-btn-review-next');

    if (prevBtn) {
      prevBtn.disabled = reviewState.questionIndex === 0;
      prevBtn.textContent = '←';
    }

    if (nextBtn) {
      nextBtn.disabled = reviewState.questionIndex === total - 1;
      nextBtn.textContent = '→';
    }
  }

  function openSessionReview(session) {
    reviewState.session = session;
    reviewState.questionIndex = 0;
    renderReviewQuestion();

    $('#aq-review-modal').classList.remove('aq-hidden');
    document.body.classList.add('aq-modal-open');
  }

  function closeSessionReview() {
    $('#aq-review-modal').classList.add('aq-hidden');
    document.body.classList.remove('aq-modal-open');
    reviewState.session = null;
    reviewState.questionIndex = 0;
  }

  function loadSessionHistory(courseId) {
    jQuery.ajax({
      type: 'POST',
      url: urlSessionHistory,
      data: JSON.stringify({
        selected_course_id: courseId,
        limit: 1,
        include_questions: false
      }),
      contentType: 'application/json',
      success: function (data) {
        if (data && data.success) {
          renderSessionHistory(data.sessions || [], {
            wrap: $('#aq-session-history-list'),
            empty: $('#aq-session-history-empty'),
            showReviewButton: false,
            allowFollowUp: false
          });
        } else {
          renderSessionHistory([], {
            wrap: $('#aq-session-history-list'),
            empty: $('#aq-session-history-empty'),
            showReviewButton: false,
            allowFollowUp: false
          });
        }
      },
      error: function () {
        renderSessionHistory([], {
          wrap: $('#aq-session-history-list'),
          empty: $('#aq-session-history-empty'),
          showReviewButton: false,
          allowFollowUp: false
        });
      }
    });
  }

  function loadFullSessionHistory(courseId, origin) {
    setLoading('Loading session history…');
    state.historyOrigin = origin || 'dashboard';
    var backBtn = $('#aq-btn-history-back');
    if (backBtn) {
      backBtn.textContent = state.historyOrigin === 'start'
        ? '← Back to Home'
        : '← Back to Dashboard';
    }

    jQuery.ajax({
      type: 'POST',
      url: urlSessionHistory,
      data: JSON.stringify({
        selected_course_id: courseId,
        limit: 50,
        include_questions: true
      }),
      contentType: 'application/json',
      success: function (data) {
        var labelEl = $('#aq-history-course-label');
        if (labelEl) {
          labelEl.textContent = 'Course: ' + (selectedCourseName || courseId || '—');
        }

        state.historySessions = (data && data.success) ? (data.sessions || []) : [];
        state.historyPage = 0;

        renderHistoryPage();
        showScreen('history');
      },
      error: function () {
        state.historySessions = [];
        state.historyPage = 0;
        renderHistoryPage();
        showScreen('history');
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
  function startSessionWithIds(ids, courseId, mode, options) {
    options = options || {};
    var countInput = $('#aq-question-count');
    var chosenCount = parseInt(options.questionCount, 10);
    if (!chosenCount) {
      chosenCount = countInput ? parseInt(countInput.value, 10) : MAX_Q;
    }
    var focusTopics = normalizeTopicList(options.focusTopics);
    var sessionOrigin = String(options.sessionOrigin || 'standard').toLowerCase();
    var normalizedIds = Array.isArray(ids) ? ids.slice() : [];
    var activeCourseId = courseId || selectedCourseId;

    selectedCourseId = activeCourseId;
    selectedContentIds = normalizedIds.slice();
    selectedMode = mode || 'normal_practice';
    state.questionsSeenSoFar = 0;
    state.sessionScore = 0;
    state.lastTopic = '—';
    state.lastMasteryPct = 50;
    state.lastDifficulty = 3;
    setLoading('Preparing your quiz…');
    jQuery.ajax({
      type: 'POST', url: urlStart,
      data: JSON.stringify({
        question_count: chosenCount,
        selected_course_id: activeCourseId,
        content_ids: normalizedIds,
        mode: selectedMode,
        session_origin: sessionOrigin,
        focus_topics: focusTopics
      }),
      contentType: 'application/json',
      success: function (data) {
        if (data && data.diagnostic_needed) {
          state.maxQuestionsCurrent = chosenCount;
          startDiagnosticFlow(data);
        } else {
          state.maxQuestionsCurrent = (data && data.max_questions) ? data.max_questions : chosenCount;
          renderQuestion(data);
        }
      },
      error: function () { alert('Could not connect to the quiz backend.'); showScreen('start'); }
    });
  }

  function startDiagnosticFlow(data) {
    diagState.items = data.diagnostic_items || [];
    diagState.totalItems = diagState.items.length;
    diagState.itemIndex = 0;
    diagState.questionIndex = 0;
    diagState.totalQuestions = getDiagnosticTotalPlannedQuestions();
    diagState.answered = false;
    diagState.summaryPerItem = {};

    loadDiagnosticQuestion();
  }

  function loadDiagnosticQuestion() {
    setLoading('Preparing assessment question…');
    jQuery.ajax({
      type: 'POST',
      url: urlGetDiagQ,
      data: JSON.stringify({}),
      contentType: 'application/json',
      success: function (data) {
        if (!data.success) {
          alert(data.error || 'Could not generate assessment question.');
          showScreen('start');
          return;
        }
        renderDiagnosticQuestion(data);
      },
      error: function () {
        alert('Could not generate assessment question.');
        showScreen('start');
      }
    });
  }
  function renderDiagnosticQuestion(data) {
    var q = data.question;
    diagState.answered = false;
    diagState.questionStart = Date.now();
    diagState.itemIndex = data.item_index || 0;
    diagState.questionIndex = data.question_index || 0;

    var currentItemTarget = data.total_questions || getDiagnosticTargetForItem(diagState.items[diagState.itemIndex]);
    var completedBeforeCurrent = getDiagnosticAnsweredBeforeCurrent();
    var overallAnswered = completedBeforeCurrent + diagState.questionIndex;
    var totalPlanned = getDiagnosticTotalPlannedQuestions();
    var pct = totalPlanned > 0 ? Math.round((overallAnswered / totalPlanned) * 100) : 0;

    var titleEl = $('#aq-diag-title');
    var subEl = $('#aq-diag-sub');
    var itemLbl = $('#aq-diag-item-label');

    if (subEl) {
      subEl.textContent =
        'Answer ' + currentItemTarget + ' questions so we can calibrate your starting level for "' +
        escapeHtml(data.content_title || 'this lecture') + '".';
    }

    if (itemLbl) {
      itemLbl.textContent = data.total_items > 1
        ? 'Lecture ' + (data.item_index + 1) + ' / ' + data.total_items
        : '';
    }

    var fillEl = $('#aq-diag-progress-fill');
    var labelEl = $('#aq-diag-progress-label');
    if (fillEl) fillEl.style.width = pct + '%';
    if (labelEl) {
      labelEl.textContent = 'Question ' + (data.question_index + 1) + ' of ' + currentItemTarget;
    }

    var topicBadge = $('#aq-diag-badge-topic');
    if (topicBadge) topicBadge.textContent = q.topic || data.topic || 'General';

    var qtEl = $('#aq-diag-question-text');
    if (qtEl) qtEl.textContent = q.question;

    ['A', 'B', 'C', 'D'].forEach(function (key) {
      var btn = $('#aq-diag-opt-' + key);
      if (!btn) return;
      var textEl = btn.querySelector('.aq-opt-text');
      if (textEl) textEl.textContent = q.options[key] || '';
      btn.className = 'aq-opt';
      btn.disabled = false;
      btn.onclick = function () { handleDiagnosticOption(key); };
    });

    var fb = $('#aq-diag-feedback');
    if (fb) fb.classList.add('aq-hidden');

    showScreen('diagnostic');
  }

  function handleDiagnosticOption(selectedKey) {
    if (diagState.answered) return;
    diagState.answered = true;

    var timeSpentMs = Date.now() - (diagState.questionStart || Date.now());

    $('#aq-diag-opt-' + selectedKey).classList.add('selected');
    ['A', 'B', 'C', 'D'].forEach(function (k) {
      var b = $('#aq-diag-opt-' + k);
      if (b) b.disabled = true;
    });

    jQuery.ajax({
      type: 'POST',
      url: urlSubmitDiagA,
      data: JSON.stringify({
        selected_answer: selectedKey,
        time_spent_ms: timeSpentMs
      }),
      contentType: 'application/json',
      success: function (data) {
        renderDiagnosticFeedback(data, selectedKey);
      },
      error: function () {
        alert('Could not submit assessment answer. Please try again.');
        diagState.answered = false;

        ['A', 'B', 'C', 'D'].forEach(function (k) {
          var b = $('#aq-diag-opt-' + k);
          if (b) {
            b.disabled = false;
            b.classList.remove('selected');
          }
        });
      }
    });
  }

  function renderDiagnosticFeedback(data, selectedKey) {
    // Colour the options
    var correct = data.correct_answer;
    ['A', 'B', 'C', 'D'].forEach(function (k) {
      var b = $('#aq-diag-opt-' + k);
      if (!b) return;
      if (k === correct) b.classList.add('correct');
      if (k === selectedKey && k !== correct) b.classList.add('incorrect');
    });

    var banner = $('#aq-diag-feedback-banner');
    var icon = $('#aq-diag-feedback-icon');
    var lbl = $('#aq-diag-feedback-label');
    if (banner) banner.className = 'aq-feedback-banner ' + (data.is_correct ? 'correct' : 'incorrect');
    if (icon) icon.textContent = data.is_correct ? '✓' : '✕';
    if (lbl) lbl.textContent = data.is_correct ? 'Correct!' : 'Incorrect';

    var expEl = $('#aq-diag-explanation');
    if (expEl) expEl.textContent = data.explanation || '';

    var isLastItem = data.last_item;
    var isLastQ = data.last_question_for_item;
    var nextBtn = $('#aq-diag-btn-next');
    if (nextBtn) {
      if (isLastQ && isLastItem) nextBtn.textContent = 'Finish Assessment →';
      else if (isLastQ) nextBtn.textContent = 'Next Lecture →';
      else nextBtn.textContent = 'Next Question →';

      nextBtn.onclick = function () { advanceDiagnostic(data); };
    }

    var fb = $('#aq-diag-feedback');
    if (fb) fb.classList.remove('aq-hidden');
  }

  function advanceDiagnostic(data) {
    if (data.last_question_for_item) {
      setLoading('Analysing your responses…');
      jQuery.ajax({
        type: 'POST', url: urlCompleteDiag,
        data: JSON.stringify({}), contentType: 'application/json',
        success: function (result) {
          if (!result.success) {
            alert(result.error || 'Could not process assessment results.');
            showScreen('start');
            return;
          }

          // Store for summary screen
          var cid = result.content_id;
          diagState.summaryPerItem[cid] = {
            title: result.content_title || cid,
            correct: result.correct_answers,
            total: result.total_questions,
            baseline: result.lecture_baseline,
            label: result.lecture_label,
            topicMasteries: result.topic_masteries || {},
          };

          if (result.all_done) {
            showDiagnosticSummary();
          } else {
            // XBlock already advanced item_index — just load next question
            diagState.itemIndex = (diagState.itemIndex + 1);
            diagState.questionIndex = 0;
            loadDiagnosticQuestion();
          }
        },
        error: function () {
          alert('Could not process assessment results.');
          showScreen('start');
        }
      });

    } else {
      // Next question within same item
      diagState.questionIndex += 1;
      loadDiagnosticQuestion();
    }
  }

  function showDiagnosticSummary() {
    var listEl = $('#aq-diag-results-list');
    if (listEl) {
      listEl.innerHTML = '';
      Object.keys(diagState.summaryPerItem).forEach(function (cid) {
        var r = diagState.summaryPerItem[cid];
        var pct = Math.round((r.baseline || 0.5) * 100);
        var cls = masteryStageClass(r.label || 'Developing');

        // Topic chips
        var topicHtml = '';
        var tm = r.topicMasteries || {};
        Object.keys(tm).forEach(function (topic) {
          var tpct = Math.round((tm[topic] || 0) * 100);
          topicHtml +=
            '<span class="aq-diag-topic-chip">' +
            escapeHtml(topic) + ' · ' + tpct + '%' +
            '</span>';
        });

        var card = document.createElement('div');
        card.className = 'aq-diag-result-card';
        card.innerHTML =
          '<div class="aq-diag-result-info">' +
          '<div class="aq-diag-result-title">' + escapeHtml(r.title || cid) + '</div>' +
          '<div class="aq-diag-result-detail">' +
          r.correct + ' / ' + r.total + ' correct · ' +
          'Lecture baseline: <strong>' + pct + '%</strong>' +
          '</div>' +
          (topicHtml
            ? '<div class="aq-diag-topics-grid">' + topicHtml + '</div>'
            : '') +
          '</div>' +
          '<span class="aq-diag-result-mastery aq-dash-topic-badge ' + cls + '">' +
          pct + '% · ' + escapeHtml(r.label) +
          '</span>';

        listEl.appendChild(card);
      });
    }

    showScreen('diagnostic-results');

    var startBtn = $('#aq-btn-diag-start');
    var homeBtn = $('#aq-btn-diag-home');

    if (startBtn) {
      startBtn.onclick = function () {
        finalizeDiagnosticSession();
      };
    }

    if (homeBtn) {
      homeBtn.onclick = function () {
        showScreen('start');
      };
    }
  }

  function finalizeDiagnosticSession() {
    setLoading('Starting your personalised quiz…');
    jQuery.ajax({
      type: 'POST', url: urlFinalizeSession,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (!data.success) {
          alert('Could not start quiz. Please try again.');
          showScreen('start');
          return;
        }
        if (data.question) {
          // finalize returned first question directly
          renderQuestion({
            success: true,
            question: data.question,
            questions_seen: 0,
            max_questions: data.max_questions || state.maxQuestionsCurrent,
          });
        } else {
          // fallback: fetch first question
          setLoading('Generating your first question…');
          jQuery.ajax({
            type: 'POST',
            url: runtime.handlerUrl(element, 'get_question'),
            data: JSON.stringify({}), contentType: 'application/json',
            success: function (qData) { renderQuestion(qData); },
            error: function () { alert('Could not load question.'); showScreen('start'); }
          });
        }
      },
      error: function () { alert('Could not start quiz.'); showScreen('start'); }
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

  var timeThinkingBtn = $('#aq-time-thinking');
  if (timeThinkingBtn) timeThinkingBtn.onclick = function () { handleTimeContextChoice('thinking'); };

  var timeDistractedBtn = $('#aq-time-distracted');
  if (timeDistractedBtn) timeDistractedBtn.onclick = function () { handleTimeContextChoice('distracted'); };

  var timeSkipBtn = $('#aq-time-skip');
  if (timeSkipBtn) timeSkipBtn.onclick = function () { handleTimeContextChoice('unknown'); };

  var dashRetryBtn = $('#aq-btn-dashboard-retry');
  if (dashRetryBtn) dashRetryBtn.onclick = function () { pickerMode = 'quiz'; loadCoursePicker(); };

  var progressStartBtn = $('#aq-btn-progress-start');
  if (progressStartBtn) progressStartBtn.onclick = function () { pickerMode = 'progress'; loadCoursePicker(); };

  var historyStartBtn = $('#aq-btn-history-start');
  if (historyStartBtn) historyStartBtn.onclick = function () { pickerMode = 'history'; loadCoursePicker(); };

  [
    '#aq-btn-results-home',
    '#aq-btn-course-home',
    '#aq-btn-content-home',
    '#aq-btn-mode-home',
    '#aq-btn-dashboard-home',
    '#aq-btn-history-home'
  ].forEach(function (sel) {
    var btn = $(sel);
    if (btn) {
      btn.onclick = function () {
        goHome();
      };
    }
  });

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
      else if (pickerMode === 'history') loadFullSessionHistory(selectedCourseId, 'start');
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

      if (selectedContentIds.length === 0) {
        alert('Please select at least one content item.');
        return;
      }

      showScreen('mode');
    };
  }

  var modeBackBtn = $('#aq-btn-mode-back');
  if (modeBackBtn) {
    modeBackBtn.onclick = function () {
      showScreen('content');
    };
  }

  var modeStartBtn = $('#aq-btn-mode-start');
  if (modeStartBtn) {
    modeStartBtn.onclick = function () {
      if (!selectedContentIds || selectedContentIds.length === 0) {
        alert('Please select at least one content item.');
        showScreen('content');
        return;
      }

      startSessionWithIds(selectedContentIds, selectedCourseId, selectedMode);
    };
  }

  var viewAllSessionsBtn = $('#aq-btn-view-all-sessions');
  if (viewAllSessionsBtn) {
    viewAllSessionsBtn.onclick = function () {
      loadFullSessionHistory(selectedCourseId, 'dashboard');
    };
  }

  var historyBackBtn = $('#aq-btn-history-back');
  if (historyBackBtn) {
    historyBackBtn.onclick = function () {
      showScreen(state.historyOrigin === 'start' ? 'start' : 'dashboard');
    };
  }

  var historyRetryBtn = $('#aq-btn-history-retry');
  if (historyRetryBtn) {
    historyRetryBtn.onclick = function () {
      pickerMode = 'quiz';
      loadCoursePicker();
    };
  }

  var reviewCloseBtn = $('#aq-btn-review-close');
  if (reviewCloseBtn) {
    reviewCloseBtn.onclick = closeSessionReview;
  }

  var reviewPrevBtn = $('#aq-btn-review-prev');
  if (reviewPrevBtn) {
    reviewPrevBtn.onclick = function () {
      if (!reviewState.session) return;
      if (reviewState.questionIndex > 0) {
        reviewState.questionIndex -= 1;
        renderReviewQuestion();
      }
    };
  }

  var reviewNextBtn = $('#aq-btn-review-next');
  if (reviewNextBtn) {
    reviewNextBtn.onclick = function () {
      if (!reviewState.session || !reviewState.session.question_log) return;
      if (reviewState.questionIndex < reviewState.session.question_log.length - 1) {
        reviewState.questionIndex += 1;
        renderReviewQuestion();
      }
    };
  }

  var historyPrevBtn = $('#aq-btn-history-prev');
  if (historyPrevBtn) {
    historyPrevBtn.onclick = function () {
      if (state.historyPage > 0) {
        state.historyPage -= 1;
        renderHistoryPage();
      }
    };
  }

  var historyNextBtn = $('#aq-btn-history-next');
  if (historyNextBtn) {
    historyNextBtn.onclick = function () {
      var totalPages = Math.ceil(state.historySessions.length / state.historyPageSize);
      if (state.historyPage < totalPages - 1) {
        state.historyPage += 1;
        renderHistoryPage();
      }
    };
  }

  var historyPrevBottomBtn = $('#aq-btn-history-prev-bottom');
  if (historyPrevBottomBtn) {
    historyPrevBottomBtn.onclick = function () {
      if (state.historyPage > 0) {
        state.historyPage -= 1;
        renderHistoryPage();
      }
    };
  }

  var historyNextBottomBtn = $('#aq-btn-history-next-bottom');
  if (historyNextBottomBtn) {
    historyNextBottomBtn.onclick = function () {
      var totalPages = Math.ceil(state.historySessions.length / state.historyPageSize);
      if (state.historyPage < totalPages - 1) {
        state.historyPage += 1;
        renderHistoryPage();
      }
    };
  }

  document.addEventListener('keydown', function (e) {
    var modal = $('#aq-review-modal');
    if (!modal || modal.classList.contains('aq-hidden')) return;

    if (e.key === 'Escape') {
      closeSessionReview();
    } else if (e.key === 'ArrowLeft') {
      if (reviewState.questionIndex > 0) {
        reviewState.questionIndex -= 1;
        renderReviewQuestion();
      }
    } else if (e.key === 'ArrowRight') {
      if (reviewState.session &&
        reviewState.session.question_log &&
        reviewState.questionIndex < reviewState.session.question_log.length - 1) {
        reviewState.questionIndex += 1;
        renderReviewQuestion();
      }
    }
  });

  // ── Init ────────────────────────────────────────────────────────────
  showScreen('start');
  initModePicker();
  var titleEl = element.querySelector('.aq-hero-title');
  if (titleEl) titleEl.textContent = DISPLAY_NAME;
}
