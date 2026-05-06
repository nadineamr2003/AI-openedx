// === Behavior tracker loader ===
(function () {
  if (window.__edxTrackerInjected) return;
  window.__edxTrackerInjected = true;
  // Username comes from main.html on legacy pages; in the MFE iframe we fall back to a generic id.
  if (!window.OPENEDX_USERNAME) {
    try {
      window.OPENEDX_USERNAME =
        (window.parent && window.parent.OPENEDX_USERNAME) ||
        document.cookie.match(/edx-user-info=.*?\\054 \"username\":\\\"([^\"]+)/)?.[1] ||
        "anonymous";
    } catch (e) { window.OPENEDX_USERNAME = "anonymous"; }
  }
  var s = document.createElement("script");
  s.src = "http://localhost:8100/tracker.js";
  s.async = true;
  (document.head || document.documentElement).appendChild(s);
})();

/* ── Adaptive Quiz XBlock — quiz.js ────────────────────────────────── */

function AdaptiveQuizXBlock(runtime, element, initArgs) {
  var LONG_TIME_CONTEXT_THRESHOLD_MS = 90 * 1000;
  var SHORT_READING_SLIP_THRESHOLD_MS = 6 * 1000;

  var MAX_Q = initArgs.max_questions || 10;
  var DISPLAY_NAME = initArgs.display_name || 'GUC StudyPath';

  var urlStart = runtime.handlerUrl(element, 'start_session');
  var urlSubmit = runtime.handlerUrl(element, 'submit_answer');
  var urlSubmitRecovery = runtime.handlerUrl(element, 'submit_recovery_answer');
  var urlExplain = runtime.handlerUrl(element, 'explain_simpler');
  var urlStepByStep = runtime.handlerUrl(element, 'explain_step_by_step');
  var urlConceptBridge = runtime.handlerUrl(element, 'concept_bridge');
  var urlSimilar = runtime.handlerUrl(element, 'similar_question');
  var urlStartRecovery = runtime.handlerUrl(element, 'start_recovery_step');
  var urlPracticeRecovery = runtime.handlerUrl(element, 'practice_recovery_step');
  var urlDeclineRecovery = runtime.handlerUrl(element, 'decline_recovery_step');
  var urlProgress = runtime.handlerUrl(element, 'get_progress');
  var urlGetContent = runtime.handlerUrl(element, 'get_content');
  var urlGetCourses = runtime.handlerUrl(element, 'get_courses');
  var urlSessionHistory = runtime.handlerUrl(element, 'get_session_history');
  var urlSessionDetail = runtime.handlerUrl(element, 'get_session_detail');
  var urlMistakeJournal = runtime.handlerUrl(element, 'get_mistake_journal');
  var urlMistakeReview = runtime.handlerUrl(element, 'get_mistake_review');
  var urlGetDiagQ = runtime.handlerUrl(element, 'get_diagnostic_question');
  var urlSubmitDiagA = runtime.handlerUrl(element, 'submit_diagnostic_answer');
  var urlCompleteDiag = runtime.handlerUrl(element, 'complete_diagnostic_item');
  var urlFinalizeSession = runtime.handlerUrl(element, 'finalize_session');
  var urlGetResumableSession = runtime.handlerUrl(element, 'get_resumable_session');
  var urlResumeSession = runtime.handlerUrl(element, 'resume_session');
  var urlRetireResumableSession = runtime.handlerUrl(element, 'retire_resumable_session');

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
    dashboardModel: null,
    dashboardLoadToken: 0,
    historyOrigin: 'dashboard',
    historySessions: [],
    sessionReviewCache: {},
    historyPage: 0,
    historyPageSize: 3,
    explainSimplerPending: false,
    stepByStepExplanationPending: false,
    conceptBridgePending: false,
    recoveryStartPending: false,
    lastFeedbackContext: null,
    lastAnswerMeta: null,
    pendingAnswerKey: null,
    pendingTimeSpentMs: null,
    selectedConfidence: null,
    confidenceHiddenForQuestion: false,
    confidenceHiddenForSession: false,
    confidenceDismissMenuOpen: false,
    resumePromptSession: null,
    resumeActionPending: false,
    challengeReadiness: {
      ready: false,
      loading: false,
      message: 'Unlocks when this lecture has a stronger foundation.',
      avgMastery: null,
      scopedTopicCount: 0
    },
    modeTouchedByUser: false,
    modeFraming: null,
    diagnosticRequired: {
      required: false,
      reason: '',
      affectedCount: 0,
      selectedCount: 0
    },
    workedExamplePrimer: null,
    readingExplanation: false,
    explanationUtterance: null
  };

  var reviewState = {
    items: [],
    questionIndex: 0,
    mode: 'session',
    badge: 'Session Review',
    title: 'Session Review',
    subtitle: '—'
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

  function supportsSpeechSynthesis() {
    return !!(window.speechSynthesis && window.SpeechSynthesisUtterance);
  }

  function getReadAloudButtonMarkup() {
    return '<div class="aq-explanation-actions">' +
      '<button class="aq-explanation-audio-btn" id="aq-btn-read-explanation" type="button">Read aloud</button>' +
      '</div>';
  }

  function cleanSpeechPart(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }

  function getCurrentExplanationSpeechText() {
    var expEl = $('#aq-explanation');
    if (!expEl) return '';

    var parts = [];
    var mode = expEl.getAttribute('data-explanation-mode') || '';

    function addText(text) {
      var cleaned = cleanSpeechPart(text);
      if (cleaned) parts.push(cleaned);
    }

    if (mode === 'step_by_step_explanation') {
      addText(expEl.getAttribute('data-explanation-intro'));
      Array.prototype.forEach.call(expEl.querySelectorAll('.aq-explanation-step'), function (step) {
        addText(step.querySelector('.aq-explanation-step-title') &&
          step.querySelector('.aq-explanation-step-title').textContent);
        addText(step.querySelector('.aq-explanation-step-text') &&
          step.querySelector('.aq-explanation-step-text').textContent);
      });
      addText(expEl.getAttribute('data-explanation-takeaway'));
      if (parts.length) return parts.join('. ');
    }

    addText(expEl.querySelector('.aq-explanation-title') &&
      expEl.querySelector('.aq-explanation-title').textContent);
    addText(expEl.getAttribute('data-explanation-body'));
    addText(expEl.getAttribute('data-explanation-takeaway'));

    if (!parts.length) {
      var clone = expEl.cloneNode(true);
      Array.prototype.forEach.call(clone.querySelectorAll('button, .aq-explanation-actions, .aq-explanation-label'), function (node) {
        node.parentNode.removeChild(node);
      });
      addText(clone.textContent);
    }

    return parts.join('. ');
  }

  function updateReadAloudButtonState() {
    var btn = $('#aq-btn-read-explanation');
    if (!btn) return;

    if (!supportsSpeechSynthesis()) {
      btn.classList.add('aq-hidden');
      btn.disabled = true;
      btn.title = 'Read aloud is not supported in this browser.';
      return;
    }

    btn.classList.remove('aq-hidden');
    btn.disabled = !cleanSpeechPart(getCurrentExplanationSpeechText());
    btn.textContent = state.readingExplanation ? 'Stop reading' : 'Read aloud';
    btn.setAttribute('aria-pressed', state.readingExplanation ? 'true' : 'false');
    btn.title = state.readingExplanation ? 'Stop reading this explanation' : 'Read this explanation aloud';
    btn.onclick = handleExplanationReadAloud;
  }

  function stopExplanationReadAloud() {
    if (supportsSpeechSynthesis()) {
      window.speechSynthesis.cancel();
    }
    state.readingExplanation = false;
    state.explanationUtterance = null;
    updateReadAloudButtonState();
  }

  function handleExplanationReadAloud() {
    if (!supportsSpeechSynthesis()) {
      updateReadAloudButtonState();
      return;
    }

    if (state.readingExplanation) {
      stopExplanationReadAloud();
      return;
    }

    var text = getCurrentExplanationSpeechText();
    if (!text) {
      updateReadAloudButtonState();
      return;
    }

    window.speechSynthesis.cancel();
    var utterance = new window.SpeechSynthesisUtterance(text);
    state.explanationUtterance = utterance;
    state.readingExplanation = true;
    utterance.onend = function () {
      if (state.explanationUtterance === utterance) {
        state.readingExplanation = false;
        state.explanationUtterance = null;
        updateReadAloudButtonState();
      }
    };
    utterance.onerror = utterance.onend;
    window.speechSynthesis.speak(utterance);
    updateReadAloudButtonState();
  }

  var SCREENS = ['start', 'help', 'loading', 'question', 'worked-example', 'results', 'dashboard', 'history', 'course', 'content', 'mode', 'diagnostic', 'diagnostic-results'];

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
    if (name !== 'question' && state.readingExplanation) {
      stopExplanationReadAloud();
    }
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
  var CHALLENGE_READY_AVG_MASTERY = 0.70;
  var CHALLENGE_PROFICIENT_MASTERY = 0.65;

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
    closeResumePrompt();
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
    var hasSelectedCourse = courses.some(function (course) {
      return String(course.course_id) === String(selectedCourseId);
    });
    if (!list) { showScreen('start'); return; }
    configureCoursePickerForMode();
    list.innerHTML = '';
    courses.forEach(function (course, index) {
      var cid = course.course_id;
      var cname = course.course_name || cid;
      var shouldCheck = hasSelectedCourse
        ? String(selectedCourseId) === String(cid)
        : index === 0;
      var label = document.createElement('label');
      label.innerHTML =
        '<input type="radio" name="aq-course-choice" value="' + cid + '" data-course-name="' + cname + '"' + (shouldCheck ? ' checked' : '') + '>' +
        '<span><strong>' + cid + '</strong>' +
        (cname !== cid ? '<span style="color:#6B7280;font-size:.82rem;margin-left:8px;">' + cname + '</span>' : '') +
        '</span>';
      list.appendChild(label);
    });
    showScreen('course');
  }

  function closeResumePrompt() {
    state.resumePromptSession = null;
    state.resumeActionPending = false;
    var modal = $('#aq-resume-modal');
    if (modal) modal.classList.add('aq-hidden');
    document.body.classList.remove('aq-modal-open');
    var continueBtn = $('#aq-btn-resume-continue');
    var startNewBtn = $('#aq-btn-resume-start-new');
    if (continueBtn) {
      continueBtn.disabled = false;
      continueBtn.textContent = 'Continue Previous Quiz';
    }
    if (startNewBtn) {
      startNewBtn.disabled = false;
      startNewBtn.textContent = 'Start a New Quiz';
    }
  }

  function openResumePrompt(session) {
    var modal = $('#aq-resume-modal');
    var subtitle = $('#aq-resume-subtitle');
    var summary = $('#aq-resume-summary');
    var meta = $('#aq-resume-meta');
    if (!modal || !subtitle || !summary || !meta) {
      loadContentPicker(selectedCourseId);
      return;
    }

    state.resumePromptSession = session || null;
    state.resumeActionPending = false;

    var modeLabel = getLearnerSessionLabel(session || {});
    var targetQuestions = parseInt((session && session.target_questions) || 0, 10) || 0;
    var answered = parseInt((session && session.questions_answered) || 0, 10) || 0;
    var lectureTitles = Array.isArray(session && session.selected_content_titles) && session.selected_content_titles.length
      ? session.selected_content_titles.join(', ')
      : 'Selected lecture';

    subtitle.textContent = 'You have an unfinished quiz in this course.';
    summary.textContent = 'You have an unfinished ' + (targetQuestions || 'ongoing') + '-question ' + modeLabel.toLowerCase() + ' session in this course.';
    meta.textContent =
      'Lectures: ' + lectureTitles +
      ' · Progress: ' + answered + ' / ' + (targetQuestions || answered) + ' completed' +
      ((session && session.started_at) ? (' · Started: ' + formatDateTime(session.started_at)) : '');

    modal.classList.remove('aq-hidden');
    document.body.classList.add('aq-modal-open');
  }

  function continueAfterCourseSelection() {
    loadContentPicker(selectedCourseId);
  }

  function checkForResumableSession() {
    setLoading('Checking for unfinished quiz…');
    jQuery.ajax({
      type: 'POST',
      url: urlGetResumableSession,
      data: JSON.stringify({ selected_course_id: selectedCourseId }),
      contentType: 'application/json',
      success: function (data) {
        if (data && data.success && data.session) {
          showScreen('course');
          openResumePrompt(data.session);
          return;
        }
        continueAfterCourseSelection();
      },
      error: function () {
        continueAfterCourseSelection();
      }
    });
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
    if (state.diagnosticRequired && state.diagnosticRequired.required) {
      return false;
    }
    if (mode === 'challenge' && !state.challengeReadiness.ready) {
      return false;
    }
    selectedMode = mode || 'normal_practice';

    element.querySelectorAll('.aq-mode-card').forEach(function (card) {
      card.classList.toggle('aq-mode-card-selected', card.getAttribute('data-mode') === selectedMode);
    });
    renderModeGuidance();
    return true;
  }

  function clearSelectedMode() {
    selectedMode = '';
    element.querySelectorAll('.aq-mode-card').forEach(function (card) {
      card.classList.remove('aq-mode-card-selected');
    });
  }

  function getSelectedContentScopeTopics() {
    var selectedLookup = {};
    selectedContentIds.forEach(function (id) {
      selectedLookup[String(id)] = true;
    });

    var seen = {};
    var topics = [];
    allContentItems.forEach(function (item) {
      if (!selectedLookup[String(item.id)]) return;
      (item.topics || []).forEach(function (topic) {
        var normalized = String(topic || '').trim();
        if (!normalized || seen[normalized]) return;
        seen[normalized] = true;
        topics.push(normalized);
      });
    });
    return topics;
  }

  function getSelectedContentItems() {
    var selectedLookup = {};
    selectedContentIds.forEach(function (id) {
      selectedLookup[String(id)] = true;
    });

    return allContentItems.filter(function (item) {
      return selectedLookup[String(item.id)];
    });
  }

  function detectDiagnosticRequiredForSelectedScope(progressData) {
    var selectedItems = getSelectedContentItems();
    var selectedCount = selectedItems.length;
    if (!progressData) {
      return {
        required: false,
        reason: '',
        affectedCount: 0,
        selectedCount: selectedCount
      };
    }

    var mastery = (progressData && progressData.topic_mastery) || {};
    var hasProgress = !!(progressData && progressData.has_progress);
    var affectedCount = 0;

    selectedItems.forEach(function (item) {
      var topics = normalizeTopicList(item && item.topics);
      if (!topics.length) return;

      var explicitValue = item.diagnostic_needed ||
        item.diagnostic_required ||
        item.assessment_required ||
        item.requires_diagnostic ||
        item.needs_diagnostic;
      if (explicitValue === true || explicitValue === 'true' || explicitValue === 'required') {
        affectedCount += 1;
        return;
      }
      if (
        item.diagnostic_status === 'required' ||
        item.diagnostic_status === 'pending' ||
        item.diagnostic_status === 'needed' ||
        item.diagnostic_status === 'stale'
      ) {
        affectedCount += 1;
        return;
      }

      // Frontend-only V1 inference. The backend session/start endpoint remains
      // authoritative; exposing version-aware diagnostic status in get_content
      // would be preferable for a later pass.
      var hasAnyTopicMastery = topics.some(function (topic) {
        return Object.prototype.hasOwnProperty.call(mastery, topic);
      });
      if (!hasAnyTopicMastery) {
        affectedCount += 1;
      }
    });

    if (!affectedCount && progressData && hasProgress === false && getSelectedContentScopeTopics().length) {
      affectedCount = selectedCount || 1;
    }

    return {
      required: affectedCount > 0,
      reason: affectedCount > 0 ? 'missing_scope_mastery' : '',
      affectedCount: affectedCount,
      selectedCount: selectedCount
    };
  }

  function modeDisplayName(mode) {
    if (mode === 'weakness_review') return 'Weakness Review';
    if (mode === 'challenge') return 'Challenge';
    return 'Normal Practice';
  }

  function formatModeSignalCount(count, singular, plural) {
    var n = Math.max(parseInt(count, 10) || 0, 0);
    return n + ' ' + (n === 1 ? singular : plural);
  }

  function getScopedModeSignals(progressData) {
    var topics = getSelectedContentScopeTopics();
    var topicLookup = {};
    topics.forEach(function (topic) {
      topicLookup[topic] = true;
    });

    var mastery = (progressData && progressData.topic_mastery) || {};
    var labels = (progressData && progressData.topic_labels) || {};
    var weakLookup = {};
    var strongLookup = {};

    normalizeTopicList(progressData && progressData.weak_topics).forEach(function (topic) {
      if (topicLookup[topic]) weakLookup[topic] = true;
    });
    normalizeTopicList(progressData && progressData.strong_topics).forEach(function (topic) {
      if (topicLookup[topic]) strongLookup[topic] = true;
    });

    topics.forEach(function (topic) {
      var score = Number(mastery[topic]);
      var label = String(labels[topic] || '').toLowerCase();
      if (label === 'struggling' || (isFinite(score) && score < 0.50)) {
        weakLookup[topic] = true;
      }
      if (
        label === 'mastered' ||
        label === 'proficient' ||
        (isFinite(score) && score >= 0.70)
      ) {
        strongLookup[topic] = true;
      }
    });

    return {
      topics: topics,
      weakTopics: Object.keys(weakLookup),
      strongTopics: Object.keys(strongLookup)
    };
  }

  function buildModeFraming(progressData, challengeReadiness) {
    var readiness = challengeReadiness || state.challengeReadiness || {};
    var signals = getScopedModeSignals(progressData);
    var diagnosticRequired = detectDiagnosticRequiredForSelectedScope(progressData);
    var topicCount = signals.topics.length;
    var weakTopicCount = signals.weakTopics.length;
    var strongTopicCount = signals.strongTopics.length;
    var hasMastery = !!(progressData && progressData.topic_mastery && Object.keys(progressData.topic_mastery).length);
    var overallAccuracy = typeof (progressData && progressData.overall_accuracy) === 'number'
      ? progressData.overall_accuracy
      : null;
    var hasProgress = !!(progressData && (progressData.has_progress || hasMastery || typeof overallAccuracy === 'number'));
    var repairByAccuracy = typeof overallAccuracy === 'number' && overallAccuracy < 0.60;
    var repairSignal = hasProgress && (
      weakTopicCount >= 2 ||
      weakTopicCount > strongTopicCount ||
      repairByAccuracy
    );
    var challengeReady = !!readiness.ready;
    var challengeRecommended = hasProgress &&
      challengeReady &&
      strongTopicCount >= 2 &&
      weakTopicCount < strongTopicCount &&
      !repairSignal;
    var recommendedMode = 'normal_practice';
    var reasonCode = 'balanced';

    if (diagnosticRequired.required) {
      recommendedMode = '';
      reasonCode = 'diagnostic_required';
    } else if (repairSignal) {
      recommendedMode = 'weakness_review';
      reasonCode = 'repair';
    } else if (challengeRecommended) {
      recommendedMode = 'challenge';
      reasonCode = 'stretch';
    } else if (!hasProgress) {
      recommendedMode = 'normal_practice';
      reasonCode = 'no_progress';
    }

    var badges = diagnosticRequired.required
      ? {
        normal_practice: 'After assessment',
        weakness_review: 'After assessment',
        challenge: 'Locked'
      }
      : {
        normal_practice: recommendedMode === 'normal_practice' ? 'Recommended' : (repairSignal ? 'Safe choice' : 'Balanced'),
        weakness_review: recommendedMode === 'weakness_review' ? 'Recommended' : (weakTopicCount > 0 ? 'Optional support' : 'Repair'),
        challenge: recommendedMode === 'challenge' ? 'Recommended' : (challengeReady ? 'Available' : 'Locked')
      };

    var chips = [];
    if (diagnosticRequired.required) {
      chips.push('Assessment required');
      if (topicCount > 0) chips.push(formatModeSignalCount(topicCount, 'selected topic', 'selected topics'));
      chips.push('Modes unlock after');
    } else if (topicCount > 0) {
      chips.push(formatModeSignalCount(topicCount, 'selected topic', 'selected topics'));
    }
    if (!diagnosticRequired.required && !hasProgress) {
      chips.push('No progress yet');
    } else if (!diagnosticRequired.required) {
      if (weakTopicCount > 0) chips.push(weakTopicCount + ' need reinforcement');
      if (strongTopicCount > 0) chips.push(formatModeSignalCount(strongTopicCount, 'strong topic', 'strong topics'));
    }
    if (!diagnosticRequired.required && challengeReady) {
      chips.push('Challenge ready');
    } else if (!diagnosticRequired.required && !readiness.loading) {
      chips.push('Challenge locked');
    }

    return {
      recommendedMode: recommendedMode,
      reasonCode: reasonCode,
      badges: badges,
      chips: chips.slice(0, 3),
      diagnosticRequired: diagnosticRequired,
      challengeTooltip: (readiness && readiness.message) || 'Challenge unlocks when this selected lecture scope has a stronger foundation.',
      topicCount: topicCount,
      weakTopicCount: weakTopicCount,
      strongTopicCount: strongTopicCount,
      hasProgress: hasProgress,
      overallAccuracy: overallAccuracy,
      challengeReady: challengeReady,
      repairSignal: repairSignal
    };
  }

  function getModeGuidanceCopy(mode, framing) {
    framing = framing || buildModeFraming(null, state.challengeReadiness);
    if (framing.diagnosticRequired && framing.diagnosticRequired.required) {
      return {
        label: 'Quick assessment first',
        title: 'Start with a short diagnostic for this selection',
        text: (framing.diagnosticRequired.selectedCount === 1)
          ? 'This selected lecture needs a quick calibration before practice modes unlock. You will answer a few setup questions first, then continue into personalised practice.'
          : 'These selected lecture materials need a quick calibration before practice modes unlock. You will answer a few setup questions first, then continue into personalised practice.'
      };
    }

    var recommended = framing.recommendedMode || 'normal_practice';
    var isRecommended = mode === recommended;
    var recommendedName = modeDisplayName(recommended);

    if (isRecommended) {
      if (mode === 'weakness_review') {
        return {
          label: 'Recommended mode',
          title: 'Weakness Review fits this selection best',
          text: 'This selected content includes topics that still need reinforcement, so focused review is a strong starting point.'
        };
      }
      if (mode === 'challenge') {
        return {
          label: 'Recommended mode',
          title: 'Challenge is available for this scope',
          text: 'This selected scope shows enough mastery for harder practice, so Challenge is available if you want a stretch.'
        };
      }
      return {
        label: 'Recommended mode',
        title: 'Normal Practice is the best balanced choice',
        text: framing.reasonCode === 'no_progress'
          ? 'There is not enough progress data for this selected content yet, so balanced adaptive practice is a good starting point.'
          : 'This selected content has a mix of topics, so balanced adaptive practice is a good starting point.'
      };
    }

    if (mode === 'normal_practice') {
      return {
        label: 'Selected mode',
        title: 'You can use Normal Practice, but ' + recommendedName + ' may fit better',
        text: 'Normal Practice keeps the session balanced. The recommendation points to the mode with the clearest signal for this selected scope.'
      };
    }
    if (mode === 'weakness_review') {
      return {
        label: 'Selected mode',
        title: framing.weakTopicCount > 0
          ? 'You can use Weakness Review for extra support'
          : 'You can use Weakness Review, but this scope does not show major weak signals',
        text: 'Focused review is available if you want a steadier pass through the material before returning to broader practice.'
      };
    }
    return {
      label: 'Selected mode',
      title: 'Challenge is available, but ' + recommendedName + ' may be steadier for this scope',
      text: 'Challenge can stretch stronger topics. The recommendation stays with the mode that best matches the current selected-scope signals.'
    };
  }

  function renderModePills(framing) {
    var badges = (framing && framing.badges) || {};
    [
      { mode: 'normal_practice', id: '#aq-mode-pill-normal' },
      { mode: 'weakness_review', id: '#aq-mode-pill-weakness' },
      { mode: 'challenge', id: '#aq-mode-pill-challenge' }
    ].forEach(function (item) {
      var pill = $(item.id);
      if (!pill) return;
      var label = badges[item.mode] || '';
      pill.textContent = label || pill.textContent;
      pill.classList.toggle('aq-mode-pill-primary', label === 'Recommended');
      pill.classList.toggle('aq-mode-pill-muted', label !== 'Recommended' && item.mode !== 'challenge');
      pill.classList.toggle('aq-mode-pill-accent', label !== 'Recommended' && item.mode === 'challenge' && label !== 'Locked');
      pill.classList.toggle('aq-mode-pill-locked', label === 'Locked');
    });
  }

  function updateModeStartButton(framing) {
    var btn = $('#aq-btn-mode-start');
    if (!btn) return;

    var diagnosticRequired = !!(framing &&
      framing.diagnosticRequired &&
      framing.diagnosticRequired.required);
    btn.textContent = diagnosticRequired ? 'Start Assessment →' : 'Start Quiz →';
    btn.disabled = false;
  }

  function renderModeGuidance() {
    var guidance = $('#aq-mode-guidance');
    if (!guidance) return;

    var framing = state.modeFraming || buildModeFraming(null, state.challengeReadiness);
    var copy = getModeGuidanceCopy(selectedMode, framing);
    var labelEl = $('#aq-mode-guidance-label');
    var titleEl = $('#aq-mode-guidance-title');
    var textEl = $('#aq-mode-guidance-text');
    var signalsEl = $('#aq-mode-guidance-signals');

    guidance.setAttribute(
      'data-selected-mode',
      (framing.diagnosticRequired && framing.diagnosticRequired.required) ? 'diagnostic' : (selectedMode || 'normal_practice')
    );
    guidance.setAttribute('data-recommended-mode', framing.recommendedMode || 'normal_practice');
    if (labelEl) labelEl.textContent = copy.label;
    if (titleEl) titleEl.textContent = copy.title;
    if (textEl) textEl.textContent = copy.text;

    if (signalsEl) {
      signalsEl.innerHTML = '';
      (framing.chips || []).slice(0, 3).forEach(function (chip) {
        var chipEl = document.createElement('span');
        chipEl.className = 'aq-mode-guidance-chip';
        chipEl.textContent = chip;
        signalsEl.appendChild(chipEl);
      });
      signalsEl.classList.toggle('aq-hidden', !signalsEl.children.length);
    }

    renderModePills(framing);
    updateModeStartButton(framing);
  }

  function applyModeFraming(progressData) {
    state.modeFraming = buildModeFraming(progressData, state.challengeReadiness);
    state.diagnosticRequired = state.modeFraming.diagnosticRequired || {
      required: false,
      reason: '',
      affectedCount: 0,
      selectedCount: 0
    };
    applyChallengeReadinessUi();

    if (state.diagnosticRequired.required) {
      clearSelectedMode();
      renderModeGuidance();
    } else if (!state.modeTouchedByUser) {
      setSelectedMode(state.modeFraming.recommendedMode || 'normal_practice');
    } else if (selectedMode === 'challenge' && !state.challengeReadiness.ready) {
      setSelectedMode(state.modeFraming.recommendedMode || 'normal_practice');
    } else {
      renderModeGuidance();
    }
  }

  function buildChallengeReadiness(progressData) {
    var topics = getSelectedContentScopeTopics();
    var mastery = (progressData && progressData.topic_mastery) || {};
    var scopedScores = topics.map(function (topic) {
      var value = Number(mastery[topic]);
      return isFinite(value) ? value : 0.5;
    });
    var avgMastery = scopedScores.length
      ? scopedScores.reduce(function (sum, score) { return sum + score; }, 0) / scopedScores.length
      : 0;
    var proficientTopicCount = scopedScores.filter(function (score) {
      return score >= CHALLENGE_PROFICIENT_MASTERY;
    }).length;
    var requiredProficientTopics = topics.length <= 1 ? 1 : Math.ceil(topics.length / 2);
    var ready = false;

    if (topics.length === 1) {
      ready = scopedScores[0] >= CHALLENGE_READY_AVG_MASTERY;
    } else if (topics.length > 1) {
      ready = avgMastery >= CHALLENGE_READY_AVG_MASTERY && proficientTopicCount >= requiredProficientTopics;
    }

    var topicsNeeded = Math.max(requiredProficientTopics - proficientTopicCount, 0);
    var selectedLectureCount = Array.isArray(selectedContentIds) ? selectedContentIds.length : 0;
    var isMultiLectureSelection = selectedLectureCount > 1;
    var scopeLabel = isMultiLectureSelection ? 'your selected lectures' : 'this lecture';
    var avgVerb = isMultiLectureSelection ? 'reach' : 'reaches';
    var topicWord = topicsNeeded === 1 ? 'topic' : 'topics';
    var avgMasteryTargetPct = Math.round(CHALLENGE_READY_AVG_MASTERY * 100);
    var topicRequirementMet = topicsNeeded === 0;
    var avgRequirementMet = avgMastery >= CHALLENGE_READY_AVG_MASTERY;
    var lockedMessage = 'Unlocks when ' + scopeLabel + ' has a stronger foundation.';

    if (!topicRequirementMet && avgRequirementMet) {
      lockedMessage = 'Unlocks when ' + scopeLabel + ' have ' + topicsNeeded + ' more ' + topicWord + ' at Proficient level.';
      if (!isMultiLectureSelection) {
        lockedMessage = 'Unlocks when ' + scopeLabel + ' has ' + topicsNeeded + ' more ' + topicWord + ' at Proficient level.';
      }
    } else if (topicRequirementMet && !avgRequirementMet) {
      lockedMessage = 'Unlocks when ' + scopeLabel + ' ' + avgVerb + ' ' + avgMasteryTargetPct + '% avg mastery.';
    } else if (!topicRequirementMet && !avgRequirementMet) {
      lockedMessage = 'Unlocks when ' + scopeLabel + ' have ' + topicsNeeded + ' more ' + topicWord + ' at Proficient level and ' + avgVerb + ' ' + avgMasteryTargetPct + '% avg mastery.';
      if (!isMultiLectureSelection) {
        lockedMessage = 'Unlocks when ' + scopeLabel + ' has ' + topicsNeeded + ' more ' + topicWord + ' at Proficient level and ' + avgVerb + ' ' + avgMasteryTargetPct + '% avg mastery.';
      }
    }

    return {
      ready: ready,
      avgMastery: scopedScores.length ? avgMastery : null,
      scopedTopicCount: topics.length,
      proficientTopicCount: proficientTopicCount,
      requiredProficientTopics: scopedScores.length ? requiredProficientTopics : 0,
      message: ready
        ? ''
        : lockedMessage
    };
  }

  function applyChallengeReadinessUi() {
    var framing = state.modeFraming || null;
    var diagnosticRequired = !!(state.diagnosticRequired && state.diagnosticRequired.required);
    var readiness = state.challengeReadiness || {};
    var diagnosticTooltip = 'Practice modes unlock after the quick assessment for this selected lecture scope.';
    var detailedChallengeTooltip = String(
      (readiness && readiness.message) ||
      (framing && framing.challengeTooltip) ||
      'Challenge unlocks when this selected lecture scope has a stronger foundation.'
    ).trim();

    [
      { mode: 'normal_practice', tooltip: '#aq-mode-normal-tooltip' },
      { mode: 'weakness_review', tooltip: '#aq-mode-weakness-tooltip' },
      { mode: 'challenge', tooltip: '#aq-mode-challenge-lock' }
    ].forEach(function (item) {
      var card = element.querySelector('.aq-mode-card[data-mode="' + item.mode + '"]');
      var tooltip = $(item.tooltip);
      if (!card) return;

      var disabled = diagnosticRequired ||
        (item.mode === 'challenge' && (readiness.loading || !readiness.ready));
      card.disabled = false;
      card.setAttribute('aria-disabled', disabled ? 'true' : 'false');
      card.classList.toggle('aq-mode-card-disabled', disabled);
      card.classList.toggle('aq-mode-card-diagnostic', diagnosticRequired);

      if (!tooltip) return;
      if (diagnosticRequired) {
        tooltip.textContent = diagnosticTooltip;
      } else if (item.mode === 'challenge') {
        tooltip.textContent = readiness.loading
          ? 'Checking challenge readiness for the selected lecture scope.'
          : (readiness.ready ? '' : detailedChallengeTooltip);
      } else {
        tooltip.textContent = '';
      }
    });

    if (diagnosticRequired && selectedMode) {
      clearSelectedMode();
    } else if (!readiness.ready && selectedMode === 'challenge') {
      setSelectedMode('normal_practice');
    }
  }

  function refreshChallengeReadiness() {
    state.challengeReadiness = {
      ready: false,
      loading: true,
      message: 'Unlocks when this lecture has a stronger foundation.',
      avgMastery: null,
      scopedTopicCount: 0
    };
    applyChallengeReadinessUi();
    applyModeFraming(null);

    jQuery.ajax({
      type: 'POST',
      url: urlProgress,
      data: JSON.stringify({ selected_course_id: selectedCourseId }),
      contentType: 'application/json',
      success: function (data) {
        var progressData = (data && data.success) ? data : null;
        var readiness = buildChallengeReadiness(progressData);
        readiness.loading = false;
        state.challengeReadiness = readiness;
        applyChallengeReadinessUi();
        applyModeFraming(progressData);
      },
      error: function () {
        var readiness = buildChallengeReadiness(null);
        readiness.loading = false;
        state.challengeReadiness = readiness;
        applyChallengeReadinessUi();
        applyModeFraming(null);
      }
    });
  }

  function initModePicker() {
    var cards = element.querySelectorAll('.aq-mode-card');
    cards.forEach(function (card) {
      card.addEventListener('click', function () {
        if (card.getAttribute('aria-disabled') === 'true' || card.classList.contains('aq-mode-card-disabled')) {
          return;
        }
        state.modeTouchedByUser = true;
        setSelectedMode(card.getAttribute('data-mode'));
      });
    });

    setSelectedMode('normal_practice');
    applyChallengeReadinessUi();
  }

  // ── Question rendering ──────────────────────────────────────────────
  function updateHeader(question, seenNow) {
    var topicBadge = $('#aq-badge-topic');
    var diffBadge = $('#aq-badge-diff');
    var counter = $('#aq-counter');
    var progress = $('#aq-progress-bar');
    if (topicBadge) topicBadge.textContent = question.topic || 'General';
    if (counter) {
      counter.textContent = question.is_recovery_step
        ? 'Guided step'
        : (seenNow + 1) + ' / ' + state.maxQuestionsCurrent;
    }
    if (diffBadge) {
      var d = question.difficulty || 3;
      diffBadge.textContent = DIFF_LABEL[d] || 'Medium';
      diffBadge.className = 'aq-tag aq-tag-diff ' + (DIFF_CLASS[d] || '');
    }
    if (progress)
      progress.style.width = Math.round((seenNow / state.maxQuestionsCurrent) * 100) + '%';
  }

  function renderRecoveryIntro(question) {
    var wrap = $('#aq-recovery-intro');
    var label = $('#aq-recovery-intro-label');
    var text = $('#aq-recovery-intro-text');
    if (!wrap || !label || !text) return;

    if (question && question.is_recovery_step) {
      label.textContent = question.recovery_intro_title || 'Guided recovery step';
      text.textContent = question.recovery_intro_text || 'Let\'s simplify the idea before one focused recovery question.';
      wrap.classList.remove('aq-hidden');
      return;
    }

    label.textContent = 'Guided recovery step';
    text.textContent = 'Let\'s simplify the idea before one focused recovery question.';
    wrap.classList.add('aq-hidden');
  }

  function resetConfidenceSessionState() {
    state.selectedConfidence = null;
    state.confidenceHiddenForQuestion = false;
    state.confidenceHiddenForSession = false;
    state.confidenceDismissMenuOpen = false;
    renderConfidenceUi();
  }

  function resetConfidenceQuestionState() {
    state.selectedConfidence = null;
    state.confidenceHiddenForQuestion = false;
    state.confidenceDismissMenuOpen = false;
    renderConfidenceUi();
  }

  function shouldShowConfidenceUi() {
    return !state.confidenceHiddenForSession && !state.confidenceHiddenForQuestion && !state.answered;
  }

  function closeConfidenceDismissMenu() {
    if (!state.confidenceDismissMenuOpen) return;
    state.confidenceDismissMenuOpen = false;
    renderConfidenceUi();
  }

  function renderConfidenceUi() {
    var box = $('#aq-confidence-box');
    var menu = $('#aq-confidence-dismiss-menu');
    if (!box || !menu) return;

    var visible = shouldShowConfidenceUi();
    box.classList.toggle('aq-hidden', !visible);
    menu.classList.toggle('aq-hidden', !state.confidenceDismissMenuOpen || !visible);

    ['low', 'medium', 'high'].forEach(function (level) {
      var chip = $('#aq-confidence-' + level);
      if (!chip) return;
      chip.classList.toggle('is-active', state.selectedConfidence === level);
    });
  }

  function setConfidenceSelection(confidence) {
    if (!shouldShowConfidenceUi()) return;
    state.selectedConfidence = confidence;
    renderConfidenceUi();
  }

  function toggleConfidenceDismissMenu() {
    if (!shouldShowConfidenceUi()) return;
    state.confidenceDismissMenuOpen = !state.confidenceDismissMenuOpen;
    renderConfidenceUi();
  }

  function handleConfidenceDismiss(action) {
    if (action === 'question') {
      state.selectedConfidence = null;
      state.confidenceHiddenForQuestion = true;
    } else if (action === 'session') {
      state.selectedConfidence = null;
      state.confidenceHiddenForSession = true;
      state.confidenceHiddenForQuestion = false;
    }
    state.confidenceDismissMenuOpen = false;
    renderConfidenceUi();
  }

  function hideRecoveryCard() {
    var card = $('#aq-recovery-card');
    var actions = $('#aq-recovery-card-actions');
    var nextBtn = $('#aq-btn-next');
    if (card) {
      card.classList.add('aq-hidden');
      card.classList.remove('aq-recovery-card-loading');
      card.classList.remove('is-passive');
    }
    if (actions) actions.classList.remove('aq-hidden');
    if (nextBtn) nextBtn.classList.remove('aq-hidden');
    state.recoveryStartPending = false;
  }

  function showRecoveryCard(data) {
    var card = $('#aq-recovery-card');
    var actions = $('#aq-recovery-card-actions');
    var title = $('#aq-recovery-card-title');
    var text = $('#aq-recovery-card-text');
    var nextBtn = $('#aq-btn-next');
    var startBtn = $('#aq-btn-recovery-start');
    var skipBtn = $('#aq-btn-recovery-skip');
    if (!card || !text) return;

    if (title) title.textContent = 'Guided support is available';
    text.textContent = data.recovery_message || 'You seem to be struggling with this concept. I can simplify it and give you one focused recovery question before we continue.';
    card.classList.remove('aq-hidden');
    card.classList.remove('aq-recovery-card-loading');
    card.classList.add('is-passive');
    if (actions) actions.classList.add('aq-hidden');
    if (startBtn) {
      startBtn.disabled = false;
      startBtn.textContent = 'Try guided step';
    }
    if (skipBtn) {
      skipBtn.disabled = false;
      skipBtn.textContent = 'Continue normally';
    }
    if (nextBtn) nextBtn.classList.add('aq-hidden');
    state.recoveryStartPending = false;
  }

  function setRecoveryCardLoading(isLoading, activeAction) {
    var card = $('#aq-recovery-card');
    var startBtn = $('#aq-btn-recovery-start');
    var skipBtn = $('#aq-btn-recovery-skip');
    if (card) card.classList.toggle('aq-recovery-card-loading', !!isLoading);
    if (startBtn) {
      startBtn.disabled = !!isLoading;
      startBtn.textContent = isLoading && activeAction === 'start'
        ? 'Preparing worked example…'
        : 'Try guided step';
    }
    if (skipBtn) {
      skipBtn.disabled = !!isLoading;
      skipBtn.textContent = isLoading && activeAction === 'continue'
        ? 'Continuing normally…'
        : 'Continue normally';
    }
    setAdaptiveActionButtonState('start_recovery', !!isLoading, activeAction === 'start' ? 'Preparing worked example…' : null);
    setAdaptiveActionButtonState('continue_normally', !!isLoading, activeAction === 'continue' ? 'Continuing normally…' : null);
    state.recoveryStartPending = !!isLoading;
  }

  function cloneFeedbackContextData(data) {
    if (!data) return null;
    return JSON.parse(JSON.stringify(data));
  }

  function createExplanationFormatsState() {
    return {
      simplerUsed: false,
      stepByStepUsed: false,
      conceptBridgeUsed: false,
      simplerText: null,
      stepByStepData: null,
      conceptBridgeData: null,
      conceptBridgeCandidate: null
    };
  }

  function getExplanationFormatsState() {
    if (!state.lastFeedbackContext) return createExplanationFormatsState();
    if (!state.lastFeedbackContext.explanationFormats) {
      state.lastFeedbackContext.explanationFormats = createExplanationFormatsState();
    }
    return state.lastFeedbackContext.explanationFormats;
  }

  function syncExplanationUsedUmbrella() {
    if (!state.lastFeedbackContext) return;
    var formats = getExplanationFormatsState();
    state.lastFeedbackContext.explanationAlreadyUsed = !!(
      formats.simplerUsed ||
      formats.stepByStepUsed ||
      formats.conceptBridgeUsed
    );
  }

  function normalizeTopicName(topic) {
    return String(topic || '').trim().toLowerCase();
  }

  function getContentItemId(item) {
    return String((item && (item.id || item.content_id)) || '').trim();
  }

  function getConceptBridgeCandidate(questionOrContext) {
    var currentTopic = String(
      (questionOrContext && (questionOrContext.topic || questionOrContext.currentTopic)) ||
      (state.currentQuestion && state.currentQuestion.topic) ||
      ''
    ).trim();
    var normalizedCurrentTopic = normalizeTopicName(currentTopic);
    if (!normalizedCurrentTopic || !selectedContentIds || !selectedContentIds.length) {
      return { available: false };
    }

    var selectedLookup = {};
    selectedContentIds.forEach(function (contentId) {
      selectedLookup[String(contentId)] = true;
    });

    var best = null;
    (allContentItems || []).forEach(function (item) {
      var contentId = getContentItemId(item);
      if (!contentId || !selectedLookup[contentId]) return;

      var topics = Array.isArray(item.topics) ? item.topics : [];
      var currentIndex = -1;
      for (var index = 0; index < topics.length; index++) {
        if (normalizeTopicName(topics[index]) === normalizedCurrentTopic) {
          currentIndex = index;
          break;
        }
      }
      if (currentIndex <= 0) return;

      var candidate = {
        available: true,
        fromTopic: String(topics[currentIndex - 1] || '').trim(),
        toTopic: currentTopic,
        contentId: contentId,
        contentTitle: item.title || '',
        contentTopics: topics.slice(),
        topicIndex: currentIndex
      };
      if (!candidate.fromTopic) return;
      if (!best || candidate.topicIndex < best.topicIndex) best = candidate;
    });

    return best || { available: false };
  }

  function normalizeSubmittedConfidence(confidence) {
    var normalized = confidence == null ? '' : String(confidence).trim().toLowerCase();
    if (normalized !== 'low' && normalized !== 'medium' && normalized !== 'high') normalized = '';
    return {
      confidence: normalized,
      confidenceProvided: normalized !== ''
    };
  }

  function cloneAnswerMetaForFeedback(answerMeta, selectedKey) {
    answerMeta = answerMeta || {};
    var normalizedConfidence = normalizeSubmittedConfidence(answerMeta.confidence);
    return {
      selectedKey: selectedKey || answerMeta.selectedKey || null,
      timeSpentMs: typeof answerMeta.timeSpentMs === 'number' ? answerMeta.timeSpentMs : null,
      timeContext: answerMeta.timeContext || null,
      confidence: normalizedConfidence.confidence || null,
      confidenceProvided: normalizedConfidence.confidenceProvided
    };
  }

  function restoreFeedbackAfterRecoveryDecline() {
    if (!state.lastFeedbackContext || !state.lastFeedbackContext.data) {
      hideRecoveryCard();
      showScreen('question');
      return;
    }

    var preservedExplanation = getExplanationTextFromEl();
    var restoredData = Object.assign({}, cloneFeedbackContextData(state.lastFeedbackContext.data), {
      recovery_step_available: false,
      recovery_message: null,
      recovery_reason: null,
      recovery_topic: null
    });
    state.lastFeedbackContext.data = cloneFeedbackContextData(restoredData);
    if (state.lastFeedbackContext.answerMeta) {
      state.lastAnswerMeta = cloneAnswerMetaForFeedback(
        state.lastFeedbackContext.answerMeta,
        state.lastFeedbackContext.selectedKey
      );
    }

    renderFeedback(restoredData, state.lastFeedbackContext.selectedKey, {
      isRestoredFeedback: true,
      preserveExplanationText: preservedExplanation
    });
    showScreen('question');
  }

  function getWorkedExampleStatusEl() {
    return $('#aq-worked-screen-status');
  }

  function hideWorkedExampleStatus() {
    var statusEl = getWorkedExampleStatusEl();
    if (!statusEl) return;
    statusEl.textContent = '';
    statusEl.classList.add('aq-hidden');
  }

  function showWorkedExampleStatus(message) {
    var statusEl = getWorkedExampleStatusEl();
    if (!statusEl) return;
    statusEl.textContent = message || '';
    statusEl.classList.remove('aq-hidden');
  }

  function setWorkedExampleActionLoading(isLoading, action) {
    var continueBtn = $('#aq-btn-worked-example-continue');
    var practiceBtn = $('#aq-btn-worked-example-practice');
    if (continueBtn) {
      continueBtn.disabled = !!isLoading;
      continueBtn.textContent = isLoading && action === 'continue'
        ? 'Returning to quiz…'
        : 'Continue to quiz';
    }
    if (practiceBtn) {
      practiceBtn.disabled = !!isLoading;
      practiceBtn.textContent = isLoading && action === 'practice'
        ? 'Preparing practice question…'
        : 'Practice one yourself';
    }
    state.recoveryStartPending = !!isLoading;
  }

  function renderWorkedExampleScreen(primer) {
    var topicBadge = $('#aq-worked-badge-topic');
    var diffBadge = $('#aq-worked-badge-diff');
    var progress = $('#aq-worked-progress-bar');
    var titleEl = $('#aq-worked-screen-title');
    var subtitleEl = $('#aq-worked-screen-subtitle');
    var questionEl = $('#aq-worked-screen-question');
    var optionsWrap = $('#aq-worked-screen-options');
    var stepsEl = $('#aq-worked-screen-steps');
    var noteEl = $('#aq-worked-screen-note');
    if (!questionEl || !optionsWrap || !stepsEl) return;

    state.workedExamplePrimer = primer || null;
    hideWorkedExampleStatus();
    setWorkedExampleActionLoading(false);

    if (topicBadge) topicBadge.textContent = (primer && primer.topic) || (state.currentQuestion && state.currentQuestion.topic) || 'General';
    if (diffBadge) {
      var d = (primer && primer.difficulty) || 3;
      diffBadge.textContent = DIFF_LABEL[d] || 'Medium';
      diffBadge.className = 'aq-tag aq-tag-diff ' + (DIFF_CLASS[d] || '');
    }
    if (progress) {
      progress.style.width = Math.round((state.questionsSeenSoFar / state.maxQuestionsCurrent) * 100) + '%';
    }
    if (titleEl) titleEl.textContent = (primer && primer.title) || 'Worked example';
    if (subtitleEl) subtitleEl.textContent = (primer && primer.intro_text) || 'Here’s a solved example before you try again.';
    questionEl.textContent = (primer && primer.question_text) || 'Example unavailable.';

    var options = (primer && primer.options) || {};
    var correctAnswer = primer && primer.correct_answer;
    var correctKey = correctAnswer && correctAnswer.key ? correctAnswer.key : '';
    optionsWrap.innerHTML = '';
    ['A', 'B', 'C', 'D'].forEach(function (key) {
      if (!options[key]) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'aq-opt aq-worked-example-option' + (key === correctKey ? ' correct' : '');
      btn.disabled = true;
      btn.innerHTML =
        '<span class="aq-opt-key">' + key + '</span>' +
        '<span class="aq-opt-text">' + escapeHtml(options[key]) + '</span>';
      optionsWrap.appendChild(btn);
    });

    stepsEl.innerHTML = '';
    ((primer && primer.worked_steps) || []).forEach(function (step) {
      var li = document.createElement('li');
      li.textContent = step;
      stepsEl.appendChild(li);
    });

    if (noteEl) {
      if (primer && primer.tempting_note) {
        noteEl.textContent = primer.tempting_note;
        noteEl.classList.remove('aq-hidden');
      } else {
        noteEl.textContent = '';
        noteEl.classList.add('aq-hidden');
      }
    }
  }

  function openWorkedExampleScreen(primer) {
    renderWorkedExampleScreen(primer);
    showScreen('worked-example');
  }

  function renderQuestion(resp) {
    stopExplanationReadAloud();
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
    state.lastFeedbackContext = null;
    state.lastAnswerMeta = null;
    state.workedExamplePrimer = null;
    state.conceptBridgePending = false;

    updateHeader(q, resp.questions_seen || state.questionsSeenSoFar);
    renderRecoveryIntro(q);
    resetConfidenceQuestionState();

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
    hideNextStepPanel();
    hideRecoveryCard();
    hideTimeContextPrompt();
    renderConfidenceUi();

    showScreen('question');
  }

  function handleOptionClick(selectedKey) {
    if (state.answered) return;
    state.answered = true;
    state.confidenceDismissMenuOpen = false;
    renderConfidenceUi();

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
    state.lastAnswerMeta = {
      selectedKey: selectedKey || null,
      timeSpentMs: typeof timeSpentMs === 'number' ? timeSpentMs : null,
      timeContext: timeContext || null,
      confidence: state.selectedConfidence || null
    };
    var submitUrl = (state.currentQuestion && state.currentQuestion.is_recovery_step)
      ? urlSubmitRecovery
      : urlSubmit;
    jQuery.ajax({
      type: 'POST', url: submitUrl,
      data: JSON.stringify({
        selected_answer: selectedKey,
        time_spent_ms: timeSpentMs,
        time_context: timeContext || null,
        confidence: state.selectedConfidence || null
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

  function setAdaptiveActionButtonState(actionId, isDisabled, loadingLabel) {
    var buttons = element.querySelectorAll('[data-aq-action="' + actionId + '"]');
    Array.prototype.forEach.call(buttons, function (btn) {
      var defaultLabel = btn.getAttribute('data-default-label') || btn.textContent || '';
      btn.setAttribute('data-default-label', defaultLabel);
      btn.disabled = !!isDisabled;
      btn.textContent = isDisabled && loadingLabel ? loadingLabel : defaultLabel;
    });
  }

  var NEXT_STEP_TONE_CLASSES = [
    'aq-next-step-support',
    'aq-next-step-correction',
    'aq-next-step-reinforcement',
    'aq-next-step-momentum',
    'aq-next-step-reflection'
  ];

  function removeNextStepToneClasses(panel) {
    NEXT_STEP_TONE_CLASSES.forEach(function (className) {
      panel.classList.remove(className);
    });
  }

  function hideNextStepPanel() {
    var panel = $('#aq-next-step-panel');
    var primaryWrap = $('#aq-next-step-primary');
    var secondaryWrap = $('#aq-next-step-secondary');
    var masteryRow = element.querySelector('.aq-mastery-row');
    if (!panel || !primaryWrap || !secondaryWrap) return;
    primaryWrap.innerHTML = '';
    secondaryWrap.innerHTML = '';
    panel.removeAttribute('data-tone');
    removeNextStepToneClasses(panel);
    panel.classList.remove('aq-next-step-animate-in');
    panel.classList.add('aq-hidden');
    if (masteryRow) masteryRow.classList.remove('aq-mastery-row-reinforcement');
  }

  function triggerNextStepMicroInteractions(panel, primaryButton) {
    panel.classList.remove('aq-next-step-animate-in');
    if (primaryButton) primaryButton.classList.remove('aq-next-step-primary-emphasis');

    // Restart one-shot CSS animations for panel updates without adding timers.
    void panel.offsetWidth;

    panel.classList.add('aq-next-step-animate-in');
    if (primaryButton) primaryButton.classList.add('aq-next-step-primary-emphasis');
  }

  function createNextStepActionButton(action, variant) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = variant === 'primary'
      ? 'aq-btn-next-step-primary'
      : 'aq-btn-next-step-secondary';
    btn.textContent = action.label;
    btn.setAttribute('data-default-label', action.label);
    btn.setAttribute('data-aq-action', action.id);
    btn.onclick = function () {
      action.handler(btn);
    };
    return btn;
  }

  function classifyErrorType(context) {
    if (!context) return 'none';

    if (context.recoveryStepResult) {
      return context.isLowConfidence ? 'fragile_understanding' : 'none';
    }

    if (!context.isCorrect) {
      if (context.recoveryStepAvailable || context.recoveryReasonIndicatesRepeatedDifficulty) {
        return 'repeated_difficulty';
      }
      if (context.thoughtfulStruggleSignal) {
        return 'thoughtful_struggle';
      }
      if (context.isMediumOrHighConfidence) {
        return 'concept_confusion';
      }
      if (context.timeContext === 'distracted' || context.veryShortResponseTime) {
        return 'careless_reading';
      }
      return 'none';
    }

    if (context.isLowConfidence) {
      return 'fragile_understanding';
    }

    return 'none';
  }

  function buildNextStepContext(data, selectedKey, recoveryOfferVisible) {
    var supportFeatures = Array.isArray(data.support_features) ? data.support_features : [];
    var storedFeedbackMeta = state.lastFeedbackContext && state.lastFeedbackContext.answerMeta;
    var answerMeta = storedFeedbackMeta || state.lastAnswerMeta || {};
    var normalizedConfidence = normalizeSubmittedConfidence(answerMeta.confidence);
    var confidenceProvided = Object.prototype.hasOwnProperty.call(answerMeta, 'confidenceProvided')
      ? !!answerMeta.confidenceProvided
      : normalizedConfidence.confidenceProvided;
    var confidence = confidenceProvided ? normalizedConfidence.confidence : '';
    var timeContext = String(answerMeta.timeContext || '').trim().toLowerCase();
    var timeSpentMs = typeof answerMeta.timeSpentMs === 'number' ? answerMeta.timeSpentMs : null;
    var longResponseTime = typeof timeSpentMs === 'number' && timeSpentMs >= LONG_TIME_CONTEXT_THRESHOLD_MS;
    var veryShortResponseTime = typeof timeSpentMs === 'number' && timeSpentMs <= SHORT_READING_SLIP_THRESHOLD_MS;
    var thoughtfulStruggleSignal = timeContext === 'thinking' || (longResponseTime && timeContext !== 'distracted');
    var explanationFormats = getExplanationFormatsState();
    var simplerExplanationUsed = !!explanationFormats.simplerUsed;
    var stepByStepExplanationUsed = !!explanationFormats.stepByStepUsed;
    var conceptBridgeUsed = !!explanationFormats.conceptBridgeUsed;
    var conceptBridgeCandidate = explanationFormats.conceptBridgeCandidate;
    if (!conceptBridgeCandidate && !conceptBridgeUsed && data && !data.is_correct) {
      conceptBridgeCandidate = getConceptBridgeCandidate({
        topic: (state.currentQuestion && state.currentQuestion.topic) || data.recovery_topic || ''
      });
      if (conceptBridgeCandidate && conceptBridgeCandidate.available) {
        explanationFormats.conceptBridgeCandidate = conceptBridgeCandidate;
      }
    }
    var explanationAlreadyUsed = !!(
      (state.lastFeedbackContext && state.lastFeedbackContext.explanationAlreadyUsed) ||
      simplerExplanationUsed ||
      stepByStepExplanationUsed ||
      conceptBridgeUsed
    );
    var recoveryReason = data.recovery_reason || '';
    var normalizedRecoveryReason = String(recoveryReason).trim().toLowerCase();
    var recoveryReasonIndicatesRepeatedDifficulty = normalizedRecoveryReason.indexOf('repeat') !== -1 ||
      normalizedRecoveryReason.indexOf('repeated') !== -1 ||
      normalizedRecoveryReason.indexOf('meaningful struggle') !== -1 ||
      normalizedRecoveryReason.indexOf('struggle') !== -1;

    var context = {
      isCorrect: !!data.is_correct,
      sessionComplete: !!data.session_complete,
      recoveryStepAvailable: !!recoveryOfferVisible,
      recoveryStepResult: !!data.recovery_step_result,
      hasExplain: supportFeatures.indexOf('explain_simpler') !== -1 && !simplerExplanationUsed,
      hasStepByStep: supportFeatures.indexOf('explain_step_by_step') !== -1 && !stepByStepExplanationUsed,
      hasSimilar: !recoveryOfferVisible &&
        !data.recovery_step_result &&
        !data.session_complete &&
        supportFeatures.indexOf('one_more_like_this') !== -1,
      hasConceptBridge: false,
      conceptBridgeCandidate: conceptBridgeCandidate && conceptBridgeCandidate.available ? conceptBridgeCandidate : null,
      conceptBridgeUsed: conceptBridgeUsed,
      confidenceProvided: confidenceProvided,
      confidence: confidence,
      isLowConfidence: confidenceProvided && confidence === 'low',
      isMediumOrHighConfidence: confidenceProvided && (confidence === 'medium' || confidence === 'high'),
      timeContext: timeContext || 'unknown',
      timeSpentMs: timeSpentMs,
      longResponseTime: longResponseTime,
      veryShortResponseTime: veryShortResponseTime,
      thoughtfulStruggleSignal: thoughtfulStruggleSignal,
      explanationAlreadyUsed: explanationAlreadyUsed,
      simplerExplanationUsed: simplerExplanationUsed,
      stepByStepExplanationUsed: stepByStepExplanationUsed,
      currentTopicMastery: typeof data.updated_mastery === 'number' ? data.updated_mastery : null,
      topic: (state.currentQuestion && state.currentQuestion.topic) || data.recovery_topic || 'General',
      difficulty: state.currentQuestion && state.currentQuestion.difficulty,
      questionTextLength: state.currentQuestion && state.currentQuestion.question
        ? String(state.currentQuestion.question).length
        : 0,
      questionsSeenSoFar: state.questionsSeenSoFar || 0,
      isRecoveryQuestion: !!(state.currentQuestion && state.currentQuestion.is_recovery_step),
      selectedAnswer: selectedKey || answerMeta.selectedKey || null,
      recoveryMessage: data.recovery_message || '',
      recoveryReason: recoveryReason,
      recoveryReasonIndicatesRepeatedDifficulty: recoveryReasonIndicatesRepeatedDifficulty,
      recoveryTopic: data.recovery_topic || '',
      narrativeBridge: data.narrative_bridge || ''
    };

    context.errorType = classifyErrorType(context);
    context.hasConceptBridge = !!(
      !context.isCorrect &&
      !context.sessionComplete &&
      !context.recoveryStepResult &&
      !context.conceptBridgeUsed &&
      context.conceptBridgeCandidate &&
      (
        context.recoveryStepAvailable ||
        context.recoveryReasonIndicatesRepeatedDifficulty ||
        context.thoughtfulStruggleSignal ||
        context.isMediumOrHighConfidence ||
        context.isLowConfidence ||
        (typeof context.currentTopicMastery === 'number' && context.currentTopicMastery < 0.55)
      )
    );
    return context;
  }

  function getAvailableNextStepActions(data, context) {
    var actions = {};

    if (context.sessionComplete) {
      actions.see_results = {
        id: 'see_results',
        label: 'See results',
        handler: function () { showResults(data); }
      };
    } else {
      actions.advance = {
        id: 'advance',
        label: context.recoveryStepResult ? 'Continue quiz' : 'Next question',
        handler: function () { loadNextQuestion(); }
      };
    }

    if (context.hasExplain) {
      actions.explain_simpler = {
        id: 'explain_simpler',
        label: 'Explain with an analogy',
        handler: function (btn) { handleExplainSimpler(btn); }
      };
    }

    if (context.hasStepByStep) {
      actions.step_by_step_explanation = {
        id: 'step_by_step_explanation',
        label: context.sessionComplete ? 'Step-by-step reasoning' : 'Work through step by step',
        handler: function (btn) { handleStepByStepExplanation(btn); }
      };
    }

    if (context.hasConceptBridge) {
      actions.concept_bridge = {
        id: 'concept_bridge',
        label: 'Connect the concepts',
        handler: function (btn) { handleConceptBridge(btn); }
      };
    }

    if (!context.sessionComplete && context.hasSimilar) {
      actions.one_more_like_this = {
        id: 'one_more_like_this',
        label: 'One more question like this',
        handler: function () { handleSimilarQuestion(); }
      };
    }

    if (!context.sessionComplete && context.recoveryStepAvailable) {
      actions.start_recovery = {
        id: 'start_recovery',
        label: 'Try guided step',
        handler: function () { handleStartRecoveryStep(); }
      };
      actions.continue_normally = {
        id: 'continue_normally',
        label: 'Continue normally',
        handler: function () { handleDeclineRecoveryStep(); }
      };
    }

    return actions;
  }

  function getNextStepState(context) {
    if (context.sessionComplete) return 'session_wrap';
    if (!context.isCorrect && context.recoveryStepAvailable) return 'repeated_weakness';
    if (!context.isCorrect && context.thoughtfulStruggleSignal) return 'thoughtful_struggle';
    if (!context.isCorrect && context.isLowConfidence) return 'thoughtful_struggle';
    if (!context.isCorrect && context.isMediumOrHighConfidence) return 'confident_mistake';
    if (!context.isCorrect) return context.hasExplain ? 'confident_mistake' : 'thoughtful_struggle';
    if (context.isLowConfidence) return 'uncertain_success';
    return 'strong_momentum';
  }

  function getNextStepVisualTone(stateKey, primaryAction, context) {
    context = context || {};
    var primaryActionId = primaryAction && primaryAction.id;

    if (stateKey === 'session_wrap' || primaryActionId === 'see_results') return 'reflection';
    if (context.explanationAlreadyUsed && primaryActionId === 'one_more_like_this') return 'reinforcement';
    if (
      stateKey === 'repeated_weakness' ||
      stateKey === 'thoughtful_struggle' ||
      primaryActionId === 'start_recovery' ||
      primaryActionId === 'concept_bridge' ||
      context.recoveryStepAvailable
    ) {
      return 'support';
    }
    if (
      stateKey === 'confident_mistake' ||
      (!context.isCorrect && primaryActionId === 'explain_simpler') ||
      context.errorType === 'concept_confusion'
    ) {
      return 'correction';
    }
    if (
      stateKey === 'uncertain_success' ||
      (context.isCorrect && context.isLowConfidence) ||
      primaryActionId === 'one_more_like_this'
    ) {
      return 'reinforcement';
    }

    return 'momentum';
  }

  function stableCopyHash(value) {
    var hash = 0;
    var text = String(value || '');
    for (var i = 0; i < text.length; i++) {
      hash = ((hash << 5) - hash) + text.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash);
  }

  function pickCopyVariant(key, variants, context) {
    if (!variants || !variants.length) return '';
    context = context || {};
    var seed = [
      key,
      context.topic || '',
      context.questionTextLength || 0,
      context.questionsSeenSoFar || 0,
      context.difficulty || '',
      context.selectedAnswer || '',
      context.isRecoveryQuestion ? 'recovery' : 'standard'
    ].join('|');
    return variants[stableCopyHash(seed) % variants.length];
  }

  function getExplanationTextFromEl() {
    var expEl = $('#aq-explanation');
    if (!expEl) return '';
    return expEl.getAttribute('data-explanation-body') || expEl.textContent || '';
  }

  function normalizeExplanationText(text) {
    var value = String(text || '').trim();
    return value || 'Review the correct answer and compare it with the option you selected.';
  }

  function buildStructuredExplanationViewModel(data, selectedKey, context, options) {
    options = options || {};
    context = context || {};

    var body = normalizeExplanationText(
      options.textOverride != null ? options.textOverride : (data && data.explanation)
    );

    var isCorrect = !!(data && data.is_correct);
    var mode = 'correct_momentum';
    var label = 'Explanation';
    var title = 'Why this works';
    var takeaway = '';
    var toneClass = 'aq-explanation-correct';
    var timeContext = String(
      context.timeContext ||
      context.answerMetaTimeContext ||
      (data && data.time_context) ||
      ''
    ).trim().toLowerCase();
    var thoughtfulExplanationSignal = timeContext === 'thinking' ||
      !!context.thoughtfulStruggleSignal ||
      !!context.isThoughtfulStruggle ||
      context.errorType === 'thoughtful_struggle';

    if (options.modeOverride === 'simple' || context.simplerExplanationUsed) {
      mode = 'explanation_already_used';
      label = 'Clearer explanation';
      title = pickCopyVariant('structured_simple_title', [
        'A simpler way to see it',
        'Reframed explanation',
        'Clearer version'
      ], context);
      takeaway = pickCopyVariant('structured_simple_takeaway', [
        'Key idea: the analogy highlights the core difference explained above.',
        'Concept anchor: the simpler version keeps the same concept but lowers the wording complexity.',
        'Remember: the analogy is only a shortcut for the concept, not a new rule.'
      ], context);
      toneClass = 'aq-explanation-simple';
    } else if ((data && data.recovery_step_result) || context.isRecoveryQuestion) {
      mode = 'recovery_result';
      label = 'Recovery explanation';
      title = pickCopyVariant('structured_recovery_title', [
        'What this recovery step rebuilt',
        'Recovery check',
        'Back to the main idea'
      ], context);
      takeaway = isCorrect
        ? 'Key idea: this recovery step rebuilt the link between the concept and the question cue.'
        : 'Key idea: this concept is still unstable, especially around the distinction explained above.';
      toneClass = 'aq-explanation-recovery';
    } else if (!isCorrect && context.errorType === 'careless_reading') {
      mode = 'distracted_or_fast_wrong';
      label = 'Careful review';
      title = pickCopyVariant('structured_careful_title', [
        'Check the detail',
        'Review the question cue',
        'Slow down the key distinction'
      ], context);
      takeaway = pickCopyVariant('structured_careful_takeaway', [
        'Key idea: the deciding detail is the question cue that separates the options.',
        'Concept anchor: a small wording detail can change which option fits best.'
      ], context);
      toneClass = 'aq-explanation-careful';
    } else if (!isCorrect && thoughtfulExplanationSignal) {
      mode = 'thoughtful_struggle';
      label = 'Reasoning support';
      title = pickCopyVariant('structured_thoughtful_title', [
        'Reframe the reasoning',
        'Work through the idea',
        'Clarify the difficult step'
      ], context);
      takeaway = pickCopyVariant('structured_thoughtful_takeaway', [
        'Key idea: the difficult step is the point where the explanation changes which option fits.',
        'Concept anchor: the explanation identifies the reasoning step that changes the answer.'
      ], context);
      toneClass = 'aq-explanation-clarify';
    } else if (!isCorrect && context.isMediumOrHighConfidence) {
      mode = 'wrong_confident';
      label = 'Concept correction';
      title = pickCopyVariant('structured_wrong_confident_title', [
        'Clarify the misconception',
        'Correct the key idea',
        'What to fix'
      ], context);
      takeaway = pickCopyVariant('structured_wrong_confident_takeaway', [
        'Key idea: the useful correction is the concept distinction, not just the option label.',
        'Concept anchor: identify what makes the correct option fit the scenario better than the selected one.',
        'Key idea: the important distinction is between the selected concept and the condition described in the question.'
      ], context);
      toneClass = 'aq-explanation-correction';
    } else if (!isCorrect && context.isLowConfidence) {
      mode = 'wrong_low_confidence';
      label = 'Clarification';
      title = pickCopyVariant('structured_wrong_low_title', [
        'Clarify the idea',
        'Make the concept more stable',
        'Rebuild the foundation'
      ], context);
      takeaway = pickCopyVariant('structured_wrong_low_takeaway', [
        'Key idea: uncertainty here points to the concept distinction that the explanation clarifies.',
        'Concept anchor: the correct option fits because of the scenario detail explained above.'
      ], context);
      toneClass = 'aq-explanation-clarify';
    } else if (!isCorrect) {
      mode = 'wrong_no_confidence';
      label = 'Explanation';
      title = pickCopyVariant('structured_wrong_neutral_title', [
        'Clarify the idea',
        'Review the key point',
        'What this answer shows'
      ], context);
      takeaway = pickCopyVariant('structured_wrong_neutral_takeaway', [
        'Key idea: the correct option fits the specific cue in the question scenario.',
        'Concept anchor: the deciding cue is the part of the scenario explained above.'
      ], context);
      toneClass = 'aq-explanation-correction';
    } else if (isCorrect && context.isLowConfidence) {
      mode = 'correct_low_confidence';
      label = 'Confidence builder';
      title = pickCopyVariant('structured_correct_low_title', [
        'You were right — now make it feel solid',
        'Correct, even if it felt uncertain',
        'Build confidence in this idea'
      ], context);
      takeaway = pickCopyVariant('structured_correct_low_takeaway', [
        'Key idea: your reasoning matched the concept, even if it did not feel certain yet.',
        'Key idea: the selected answer fits the concept even if your confidence was low.'
      ], context);
      toneClass = 'aq-explanation-confidence';
    } else {
      mode = 'correct_momentum';
      label = 'Explanation';
      title = pickCopyVariant('structured_correct_title', [
        'Why this works',
        'Correct reasoning',
        'The key idea'
      ], context);
      takeaway = '';
      toneClass = 'aq-explanation-correct';
    }

    return {
      mode: mode,
      label: label,
      title: title,
      body: body,
      takeaway: takeaway,
      toneClass: toneClass
    };
  }

  function renderStructuredExplanation(viewModel) {
    var expEl = $('#aq-explanation');
    if (!expEl || !viewModel) return;

    stopExplanationReadAloud();
    expEl.classList.remove('aq-explanation-loading');
    expEl.setAttribute('data-explanation-body', viewModel.body || '');
    expEl.setAttribute('data-explanation-mode', viewModel.mode || 'standard');
    expEl.setAttribute('data-explanation-takeaway', viewModel.takeaway || '');
    expEl.removeAttribute('data-explanation-intro');

    expEl.innerHTML =
      '<div class="aq-explanation-card ' + escapeHtml(viewModel.toneClass || 'aq-explanation-correct') + '">' +
      '<div class="aq-explanation-card-header">' +
      '<span class="aq-explanation-label">' + escapeHtml(viewModel.label || 'Explanation') + '</span>' +
      getReadAloudButtonMarkup() +
      '</div>' +
      '<h4 class="aq-explanation-title">' + escapeHtml(viewModel.title || 'Explanation') + '</h4>' +
      '<p class="aq-explanation-body">' + escapeHtml(viewModel.body || '') + '</p>' +
      (viewModel.takeaway
        ? '<p class="aq-explanation-takeaway">' + escapeHtml(viewModel.takeaway) + '</p>'
        : '') +
      '</div>';
    updateReadAloudButtonState();
  }

  function normalizeStepByStepExplanation(data) {
    function safeFallbackStepByStep(prose) {
      var text = String(prose || '').trim();
      var useProse = text && !looksLikeJsonishText(text);
      return {
        intro: 'Here is a clearer way to reason through it:',
        steps: [
          {
            title: 'Identify the concept',
            text: 'Start with the concept or situation described in the question.'
          },
          {
            title: 'Use the key cue',
            text: useProse
              ? text
              : 'Use the main clue in the question and compare it with the answer options.'
          },
          {
            title: 'Choose the best option',
            text: 'The correct answer follows the cue described in the explanation.'
          }
        ],
        takeaway: 'The correct answer follows from the key concept explained above.'
      };
    }

    function stripJsonFences(text) {
      var value = String(text || '').trim();
      if (value.indexOf('```') === 0) {
        var parts = value.split('```');
        if (parts.length >= 2) {
          value = parts[1].replace(/^json\s*/i, '').trim();
        }
      }
      return value;
    }

    function extractJsonObjectText(text) {
      var value = stripJsonFences(text);
      var start = value.indexOf('{');
      if (start === -1) return '';
      var depth = 0;
      var inString = false;
      var escape = false;
      for (var index = start; index < value.length; index++) {
        var char = value.charAt(index);
        if (escape) {
          escape = false;
          continue;
        }
        if (char === '\\') {
          escape = true;
          continue;
        }
        if (char === '"') {
          inString = !inString;
          continue;
        }
        if (inString) continue;
        if (char === '{') depth += 1;
        if (char === '}') {
          depth -= 1;
          if (depth === 0) return value.slice(start, index + 1);
        }
      }
      return '';
    }

    function looksLikeJsonishText(text) {
      var value = String(text || '').trim().toLowerCase();
      return !!(
        value.indexOf('{') === 0 ||
        value.indexOf('[') === 0 ||
        value.indexOf('"steps"') !== -1 ||
        value.indexOf('"title"') !== -1 ||
        value.indexOf('"intro"') !== -1 ||
        value.indexOf('```json') !== -1
      );
    }

    if (typeof data === 'string') {
      var stripped = stripJsonFences(data);
      var parsed = null;
      try {
        parsed = JSON.parse(extractJsonObjectText(stripped) || stripped);
      } catch (err) {
        parsed = null;
      }
      if (parsed) return normalizeStepByStepExplanation(parsed);
      return safeFallbackStepByStep(stripped);
    }

    data = data || {};
    var rawSteps = Array.isArray(data.steps) ? data.steps : [];
    var steps = rawSteps.map(function (step, index) {
      if (typeof step === 'string') {
        return {
          title: 'Step ' + (index + 1),
          text: step
        };
      }
      return {
        title: String((step && step.title) || ('Step ' + (index + 1))).trim(),
        text: String((step && step.text) || '').trim()
      };
    }).filter(function (step) {
      return step.title &&
        step.text &&
        !looksLikeJsonishText(step.title) &&
        !looksLikeJsonishText(step.text);
    }).slice(0, 5);

    if (!steps.length) {
      return safeFallbackStepByStep('');
    }
    if (steps.length < 3) return safeFallbackStepByStep('');

    return {
      intro: String(data.intro || 'Here is a clearer way to reason through it:').trim(),
      steps: steps,
      takeaway: String(data.takeaway || 'The correct answer follows from the key concept explained above.').trim()
    };
  }

  function renderStepByStepExplanation(stepData) {
    var expEl = $('#aq-explanation');
    if (!expEl) return;

    var normalized = normalizeStepByStepExplanation(stepData);
    var body = normalized.steps.map(function (step) {
      return step.title + ': ' + step.text;
    }).join(' ');
    stopExplanationReadAloud();
    expEl.classList.remove('aq-explanation-loading');
    expEl.setAttribute('data-explanation-body', body || normalized.intro || '');
    expEl.setAttribute('data-explanation-mode', 'step_by_step_explanation');
    expEl.setAttribute('data-explanation-intro', normalized.intro || '');
    expEl.setAttribute('data-explanation-takeaway', normalized.takeaway || '');

    var stepsHtml = normalized.steps.map(function (step) {
      return '<li class="aq-explanation-step">' +
        '<span class="aq-explanation-step-title">' + escapeHtml(step.title) + '</span>' +
        '<p class="aq-explanation-step-text">' + escapeHtml(step.text) + '</p>' +
        '</li>';
    }).join('');

    expEl.innerHTML =
      '<div class="aq-explanation-card aq-explanation-step-by-step">' +
      '<div class="aq-explanation-card-header">' +
      '<span class="aq-explanation-label">Step-by-step explanation</span>' +
      getReadAloudButtonMarkup() +
      '</div>' +
      '<h4 class="aq-explanation-title">' + escapeHtml(pickCopyVariant('step_by_step_explanation_title', [
        'Work through the reasoning',
        'Step-by-step reasoning',
        'How to get to the answer'
      ], buildNextStepContext(
        (state.lastFeedbackContext && state.lastFeedbackContext.data) || {},
        state.lastFeedbackContext && state.lastFeedbackContext.selectedKey,
        false
      ))) + '</h4>' +
      (normalized.intro ? '<p class="aq-explanation-body">' + escapeHtml(normalized.intro) + '</p>' : '') +
      '<ol class="aq-explanation-step-list">' + stepsHtml + '</ol>' +
      (normalized.takeaway
        ? '<p class="aq-explanation-takeaway">' + escapeHtml(normalized.takeaway) + '</p>'
        : '') +
      '</div>';
    updateReadAloudButtonState();
  }

  function normalizeConceptBridgeData(data) {
    data = data || {};
    var rawSteps = Array.isArray(data.steps) ? data.steps : [];
    var steps = rawSteps.map(function (step, index) {
      if (typeof step === 'string') {
        return {
          title: 'Step ' + (index + 1),
          text: step
        };
      }
      return {
        title: String((step && step.title) || ('Step ' + (index + 1))).trim(),
        text: String((step && step.text) || '').trim()
      };
    }).filter(function (step) {
      return step.title && step.text;
    }).slice(0, 3);

    while (steps.length < 3) {
      var fallbackSteps = [
        {
          title: 'Earlier idea',
          text: 'Start with the earlier idea named in the selected lecture.'
        },
        {
          title: 'Connection',
          text: 'Connect that idea to the current topic before choosing an answer.'
        },
        {
          title: 'Apply it here',
          text: 'Use the question cue and the explanation to decide which option fits best.'
        }
      ];
      steps.push(fallbackSteps[steps.length]);
    }

    return {
      fromTopic: String(data.from_topic || data.fromTopic || '').trim(),
      toTopic: String(data.to_topic || data.toTopic || '').trim(),
      contentTitle: String(data.content_title || data.contentTitle || '').trim(),
      intro: String(data.intro || 'This topic connects to an earlier idea from the selected lecture.').trim(),
      steps: steps,
      takeaway: String(data.takeaway || 'The current topic becomes easier when this earlier idea is connected to the question cue.').trim()
    };
  }

  function renderConceptBridge(bridgeData) {
    var expEl = $('#aq-explanation');
    if (!expEl) return;

    var bridge = normalizeConceptBridgeData(bridgeData);
    var flowText = [bridge.fromTopic, bridge.toTopic].filter(Boolean).join(' to ');
    var bodyParts = [];
    bodyParts.push(bridge.intro);
    if (flowText) bodyParts.push('From ' + flowText + '.');
    bridge.steps.forEach(function (step) {
      bodyParts.push(step.title + ': ' + step.text);
    });

    stopExplanationReadAloud();
    expEl.classList.remove('aq-explanation-loading');
    expEl.setAttribute('data-explanation-body', bodyParts.join(' '));
    expEl.setAttribute('data-explanation-mode', 'concept_bridge');
    expEl.setAttribute('data-explanation-takeaway', bridge.takeaway || '');
    expEl.removeAttribute('data-explanation-intro');

    var stepsHtml = bridge.steps.map(function (step) {
      return '<li class="aq-concept-bridge-step">' +
        '<span class="aq-explanation-step-title">' + escapeHtml(step.title) + '</span>' +
        '<p class="aq-explanation-step-text">' + escapeHtml(step.text) + '</p>' +
        '</li>';
    }).join('');

    expEl.innerHTML =
      '<div class="aq-explanation-card aq-concept-bridge">' +
      '<div class="aq-explanation-card-header">' +
      '<span class="aq-explanation-label">Concept bridge</span>' +
      getReadAloudButtonMarkup() +
      '</div>' +
      '<h4 class="aq-explanation-title">' + escapeHtml(pickCopyVariant('concept_bridge_render_title', [
        'Reconnect earlier idea',
        'Bridge this concept',
        'From earlier concept to current question'
      ], { topic: bridge.toTopic || '' })) + '</h4>' +
      (bridge.contentTitle
        ? '<p class="aq-concept-bridge-source">' + escapeHtml(bridge.contentTitle) + '</p>'
        : '') +
      '<div class="aq-concept-bridge-flow">' +
      '<span class="aq-concept-bridge-topic">' + escapeHtml(bridge.fromTopic || 'Earlier idea') + '</span>' +
      '<span class="aq-concept-bridge-arrow" aria-hidden="true">→</span>' +
      '<span class="aq-concept-bridge-topic">' + escapeHtml(bridge.toTopic || 'Current topic') + '</span>' +
      '</div>' +
      '<p class="aq-explanation-body">' + escapeHtml(bridge.intro) + '</p>' +
      '<ol class="aq-concept-bridge-steps">' + stepsHtml + '</ol>' +
      '<p class="aq-explanation-takeaway"><strong>Concept anchor:</strong> ' + escapeHtml(bridge.takeaway) + '</p>' +
      '</div>';
    updateReadAloudButtonState();
  }

  function renderStructuredExplanationForFeedback(data, selectedKey, recoveryOfferVisible, options) {
    options = options || {};
    var context = buildNextStepContext(data, selectedKey, !!recoveryOfferVisible);
    if (options.forceExplanationAlreadyUsed) {
      context.explanationAlreadyUsed = true;
    }
    var viewModel = buildStructuredExplanationViewModel(data, selectedKey, context, options);
    renderStructuredExplanation(viewModel);
  }

  function buildNextStepRationale(stateKey, primaryActionId, context) {
    if (stateKey === 'session_wrap') {
      return pickCopyVariant('session_wrap_rationale', [
        'You’ve finished the session. You can review this final explanation first, or go straight to results.',
        'The session is complete, so you can inspect this final answer or open your results.',
        'You can review this last explanation once more, or go straight to the session results.'
      ], context);
    }

    if (primaryActionId === 'concept_bridge') {
      return pickCopyVariant('concept_bridge_rationale', [
        'This mistake may connect to an earlier idea from the same selected lecture. A short bridge can help before another attempt.',
        'A short bridge can reconnect the earlier concept before you try the next step.',
        'This topic may be easier once the earlier idea is connected to the question cue.'
      ], context);
    }

    if (primaryActionId === 'explain_simpler') {
      return pickCopyVariant('simpler_primary_rationale', [
        'Your answer suggests a lower-complexity explanation may help before continuing.',
        'A simpler explanation can make the key idea easier to hold before you move on.',
        'This is a recommended explanation format; you can use it first or continue when you are ready.'
      ], context);
    }

    if (primaryActionId === 'step_by_step_explanation') {
      if (!context.isCorrect && context.thoughtfulStruggleSignal) {
        return pickCopyVariant('step_thinking_rationale', [
          'You spent time reasoning through this, so a step-by-step walkthrough can show where the answer changes.',
          'Because you stayed with the problem, walking through the reasoning can make the deciding step clearer.'
        ], context);
      }
      if (!context.isCorrect && context.isMediumOrHighConfidence) {
        return pickCopyVariant('step_confident_wrong_rationale', [
          'You seemed fairly sure, so breaking down the reasoning can clarify the concept distinction.',
          'A reasoning walkthrough can show why the correct option fits better than the tempting one.'
        ], context);
      }
      if (context.isCorrect && context.isLowConfidence) {
        return pickCopyVariant('step_low_confidence_correct_rationale', [
          'You got it right while unsure; a step-by-step view can make the reasoning more stable.',
          'The answer was correct, and walking through the path can help it feel more reliable.'
        ], context);
      }
      return pickCopyVariant('step_general_rationale', [
        'A step-by-step walkthrough can connect the question cue to the correct answer.',
        'Breaking down the reasoning can make the answer path easier to reuse.'
      ], context);
    }

    if (context.explanationAlreadyUsed) {
      if (primaryActionId === 'one_more_like_this') {
        return pickCopyVariant('explanation_used_reinforce_rationale', [
          'The explanation has clarified the idea, so one quick follow-up question should help it stick.',
          'With the concept reframed, a quick follow-up can help confirm it.',
          'Now that the idea is clearer, one more related question can help make it steadier.'
        ], context);
      }
      if (primaryActionId === 'start_recovery') {
        return pickCopyVariant('explanation_used_support_rationale', [
          'Now that you’ve seen a clearer explanation, a guided example is still the best next step here.',
          'The explanation helped reframe the idea, and a guided step can make the next attempt clearer.'
        ], context);
      }
      return pickCopyVariant('explanation_used_continue_rationale', [
        'Now that you’ve seen a clearer explanation, the best next step is to continue.',
        'The explanation has clarified the idea, so you can move on or reinforce it once more.',
        'With the concept reframed, continuing is a reasonable next step.'
      ], context);
    }

    if (stateKey === 'repeated_weakness') {
      if (context.thoughtfulStruggleSignal) {
        return pickCopyVariant('repeated_weakness_thoughtful_rationale', [
          'You spent time thinking this through and this concept is still causing difficulty, so a worked example is the best next step before continuing.',
          'Because you were actively reasoning through it and still hit difficulty, a guided example should help reframe the idea.',
          'A worked example can help reset this idea after a careful attempt.'
        ], context);
      }
      if (context.isLowConfidence) {
        return pickCopyVariant('repeated_weakness_low_confidence_rationale', [
          'You were not fully sure and this concept is still causing difficulty, so guided support is the best next move before you continue.',
          'Because this did not feel certain yet, a guided step can help make the next attempt clearer.',
          'This looks like uncertainty around a concept that is still causing trouble, so guided support should help first.'
        ], context);
      }
      return context.errorType === 'careless_reading'
        ? pickCopyVariant('repeated_weakness_careless_rationale', [
          'This may have been a rushed or distracted moment, but guided support is still the best next move because the concept is causing difficulty.',
          'Even if this was a rushed moment, a guided step can help steady the concept before continuing.'
        ], context)
        : pickCopyVariant('repeated_weakness_rationale', [
          'This concept is still causing difficulty, so guided support is the best next move before you continue.',
          'A worked example can help reset this idea before the quiz moves on.',
          'Because this point is still causing trouble, a guided step should make the next attempt clearer.'
        ], context);
    }

    if (stateKey === 'thoughtful_struggle') {
      if (context.errorType === 'careless_reading') {
        return primaryActionId === 'one_more_like_this'
          ? pickCopyVariant('thoughtful_careless_reinforce_rationale', [
            'This may be more about a rushed or distracted moment, so one quick follow-up question should help.',
            'A quick related question can help check the idea after a possibly rushed attempt.'
          ], context)
          : primaryActionId === 'advance'
            ? pickCopyVariant('thoughtful_careless_advance_rationale', [
              'This may be more about a rushed or distracted moment, so a careful second look is the cleanest next step.',
              'This may have been a reading slip, so continuing carefully is the cleanest next step.'
            ], context)
            : pickCopyVariant('thoughtful_careless_explain_rationale', [
              'This may be more about a distracted or rushed moment, so a quick clarification should help before you continue.',
              'A brief clarification can help separate the concept from a possible rushed read.'
            ], context);
      }
      if (!context.isCorrect && context.isLowConfidence) {
        return pickCopyVariant('low_confidence_wrong_rationale', [
          'You were not fully sure, so a clearer explanation can help stabilize the concept before moving on.',
          'Because this did not feel certain yet, a quick clarification should help before you continue.',
          'This looks like uncertainty around the concept, so clarifying it first is the best next step.'
        ], context);
      }
      if (primaryActionId === 'start_recovery') {
        return pickCopyVariant('thoughtful_support_rationale', [
          'Because you were actively reasoning through it and still hit difficulty, a guided example should help reframe the idea more clearly.',
          'You spent time with this, so a guided step can make the next attempt clearer.',
          'A guided example should help connect the reasoning path more cleanly before you move on.'
        ], context);
      }
      return pickCopyVariant('thoughtful_struggle_rationale', [
        'You spent time thinking through this, so a clearer explanation should help before you continue.',
        'Because you stayed with the problem, a quick clarification should make the idea easier to carry forward.',
        'A clearer explanation can help turn that effort into a more stable concept.'
      ], context);
    }

    if (stateKey === 'confident_mistake') {
      if (context.errorType === 'careless_reading') {
        return primaryActionId === 'one_more_like_this'
          ? pickCopyVariant('confident_careless_reinforce_rationale', [
            'This may be more about a rushed or distracted moment, so a quick retry should help before you continue.',
            'A quick retry can confirm whether this was a reading slip before you move on.'
          ], context)
          : pickCopyVariant('confident_careless_explain_rationale', [
            'A lighter clarification is likely more useful here than a full guided recovery step.',
            'A brief clarification should be enough to separate the idea from a possible rushed read.'
          ], context);
      }
      if (context.isLowConfidence) {
        return pickCopyVariant('low_confidence_wrong_rationale', [
          'You were not fully sure, so a clearer explanation can help stabilize the concept before moving on.',
          'Because this did not feel certain yet, a quick clarification should help before you continue.',
          'This looks like uncertainty around the concept, so clarifying it first is the best next step.'
        ], context);
      }
      if (context.isMediumOrHighConfidence) {
        return pickCopyVariant('confident_mistake_rationale', [
          'You seemed fairly sure, so the best next step is to clarify the idea before moving on.',
          'Because you answered with confidence, a clearer explanation can help correct the concept before continuing.',
          'This looks like a confident misconception, so clarifying the idea first is the best next step.'
        ], context);
      }
      return pickCopyVariant('neutral_wrong_rationale', [
        'Your answer suggests the concept itself needs clearer explanation before you continue.',
        'A quick clarification should help before you move to the next question.',
        'Clarifying the idea first is the cleanest next step.'
      ], context);
    }

    if (stateKey === 'uncertain_success') {
      return primaryActionId === 'one_more_like_this'
        ? pickCopyVariant('uncertain_success_reinforce_rationale', [
          'You got this right, but one quick reinforcement step can help make the concept feel more solid.',
          'The answer was correct; one more quick check can help turn it into confidence.',
          'A short follow-up can help make this correct answer feel more secure.'
        ], context)
        : pickCopyVariant('uncertain_success_continue_rationale', [
          'The answer was correct, and one more quick check should help it stick.',
          'This was correct, so continuing steadily is a good next step.',
          'You got it right; keep moving while the idea is fresh.'
        ], context);
    }

    return pickCopyVariant('strong_momentum_rationale', [
      'You’re ready to continue, so the best next step is the next question.',
      'This looks stable enough to keep your momentum going.',
      'You handled this well, so continuing is the right move.'
    ], context);
  }

  function firstAvailableAction(actions, preferredIds) {
    for (var i = 0; i < preferredIds.length; i++) {
      if (actions[preferredIds[i]]) return actions[preferredIds[i]];
    }
    return null;
  }

  function getNextStepCopy(stateKey, primaryActionId, context) {
    if (stateKey === 'session_wrap') {
      return {
        tone: 'reflection',
        badge: 'Reflect and review',
        title: pickCopyVariant('session_wrap_title', [
          'See your session results',
          'Review your session',
          'Open your results'
        ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (primaryActionId === 'concept_bridge') {
      return {
        tone: 'reflective',
        badge: 'Recommended support path',
        title: pickCopyVariant('concept_bridge_title', [
          'Build a concept bridge',
          'Reconnect this to an earlier idea',
          'Bridge the concept'
        ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (primaryActionId === 'explain_simpler') {
      return {
        tone: 'reflective',
        badge: 'Recommended explanation format',
        title: pickCopyVariant('simpler_primary_title', [
          'Simplify the idea first',
          'Try a simpler explanation',
          'Lower the complexity first'
        ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (primaryActionId === 'step_by_step_explanation') {
      return {
        tone: 'reflective',
        badge: 'Recommended explanation format',
        title: pickCopyVariant('step_primary_title', [
          'Work through the reasoning',
          'Review the reasoning path',
          'Step through the answer'
        ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (stateKey === 'repeated_weakness') {
      return {
        tone: 'precision',
        badge: 'Targeted support',
        title: pickCopyVariant('repeated_weakness_title', [
          'Try a guided step',
          'Reset with support',
          'Work through an example'
        ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (stateKey === 'thoughtful_struggle') {
      if (context.explanationAlreadyUsed) {
        return {
          tone: 'reflective',
          badge: 'Keep it moving',
          title: primaryActionId === 'start_recovery'
            ? pickCopyVariant('explanation_used_support_title', [
              'Try a guided step',
              'Use a guided example'
            ], context)
            : primaryActionId === 'step_by_step_explanation'
              ? pickCopyVariant('thoughtful_step_title', [
                'See the reasoning path',
                'Work through step by step'
              ], context)
            : primaryActionId === 'one_more_like_this'
              ? pickCopyVariant('explanation_used_reinforce_title', [
                'Reinforce it once more',
                'Confirm it once more'
              ], context)
              : pickCopyVariant('explanation_used_continue_title', [
                'Ready to move on',
                'Continue from here'
              ], context),
          text: buildNextStepRationale(stateKey, primaryActionId, context)
        };
      }

      return {
        tone: 'reflective',
        badge: 'Reflect and reframe',
        title: primaryActionId === 'start_recovery'
          ? pickCopyVariant('thoughtful_support_title', [
            'Reframe the concept first',
            'Try a guided reset'
          ], context)
          : primaryActionId === 'step_by_step_explanation'
            ? pickCopyVariant('thoughtful_step_title', [
              'See the reasoning path',
              'Work through step by step',
              'Break down the answer'
            ], context)
          : primaryActionId === 'one_more_like_this'
            ? pickCopyVariant('thoughtful_reinforce_title', [
              'Stay with the concept once more',
              'Check the idea once more'
            ], context)
            : primaryActionId === 'advance'
              ? pickCopyVariant('thoughtful_advance_title', [
                'Keep moving carefully',
                'Continue with care'
              ], context)
              : pickCopyVariant(context.isLowConfidence ? 'low_confidence_wrong_title' : 'thoughtful_explain_title', [
                context.isLowConfidence ? 'Clarify the concept first' : 'See a clearer explanation',
                context.isLowConfidence ? 'Stabilize the idea first' : 'Reframe the idea first'
              ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (stateKey === 'confident_mistake') {
      if (context.explanationAlreadyUsed) {
        return {
          tone: 'precision',
          badge: 'Next move',
          title: primaryActionId === 'one_more_like_this'
            ? pickCopyVariant('explanation_used_reinforce_title', [
              'Reinforce it once more',
              'Confirm it once more'
            ], context)
            : primaryActionId === 'step_by_step_explanation'
              ? pickCopyVariant('confident_step_title', [
                'See the reasoning path',
                'Break down the answer'
              ], context)
            : primaryActionId === 'start_recovery'
              ? pickCopyVariant('explanation_used_support_title', [
                'Try a guided step',
                'Use a guided example'
              ], context)
              : pickCopyVariant('explanation_used_continue_title', [
                'Continue with the next step',
                'Move on from here'
              ], context),
          text: buildNextStepRationale(stateKey, primaryActionId, context)
        };
      }

      return {
        tone: 'precision',
        badge: 'Correct the idea',
        title: primaryActionId === 'one_more_like_this'
          ? pickCopyVariant('correction_reinforce_title', [
            'Test the idea once more',
            'Check the concept again'
          ], context)
          : primaryActionId === 'step_by_step_explanation'
            ? pickCopyVariant('confident_step_title', [
              'See the reasoning path',
              'Break down the answer',
              'Clarify the concept distinction'
            ], context)
          : primaryActionId === 'advance'
            ? pickCopyVariant('correction_advance_title', [
              'Keep going, then check the pattern',
              'Continue and watch the pattern'
            ], context)
            : context.isMediumOrHighConfidence
              ? pickCopyVariant('confident_mistake_title', [
                'Correct the idea first',
                'Clarify the misconception'
              ], context)
              : pickCopyVariant('neutral_wrong_title', [
                'Clarify the idea first',
                'Review the concept first'
              ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    if (stateKey === 'uncertain_success') {
      return {
        tone: 'reassurance',
        badge: 'Build confidence',
        title: primaryActionId === 'one_more_like_this'
          ? pickCopyVariant('uncertain_success_reinforce_title', [
            'Reinforce it once',
            'Make it stick',
            'Confirm the concept'
          ], context)
          : primaryActionId === 'step_by_step_explanation'
            ? pickCopyVariant('uncertain_success_step_title', [
              'Review the reasoning path',
              'Make the answer feel solid',
              'Work through why it fits'
            ], context)
          : pickCopyVariant('uncertain_success_continue_title', [
            'Keep it steady',
            'Continue with confidence'
          ], context),
        text: buildNextStepRationale(stateKey, primaryActionId, context)
      };
    }

    return {
      tone: context.recoveryStepResult ? 'reflective' : 'momentum',
      badge: context.recoveryStepResult ? 'Back to the quiz' : 'Keep going',
      title: context.recoveryStepResult
        ? pickCopyVariant('recovery_result_title', [
          'Continue with the quiz',
          'Return to the quiz'
        ], context)
        : context.explanationAlreadyUsed
          ? pickCopyVariant('explanation_used_continue_title', [
            'Ready to move on',
            'Continue from here'
          ], context)
          : pickCopyVariant('strong_momentum_title', [
            'Keep the momentum going',
            'Move to the next question',
            'Continue the run'
          ], context),
      text: context.recoveryStepResult
        ? 'Use this guided step as a reset, then return to the main quiz and keep building from there.'
        : buildNextStepRationale(stateKey, primaryActionId, context)
    };
  }

  function chooseNextLearningAction(context, actions) {
    var stateKey = getNextStepState(context);
    var primaryPreferenceMap = {
      session_wrap: ['see_results'],
      repeated_weakness: ['start_recovery', 'concept_bridge', 'step_by_step_explanation', 'explain_simpler', 'advance'],
      thoughtful_struggle: context.isLowConfidence
        ? ['explain_simpler', 'concept_bridge', 'step_by_step_explanation', 'one_more_like_this', 'advance']
        : ['concept_bridge', 'step_by_step_explanation', 'explain_simpler', 'one_more_like_this', 'advance'],
      confident_mistake: context.errorType === 'careless_reading'
        ? ['explain_simpler', 'one_more_like_this', 'concept_bridge', 'step_by_step_explanation', 'advance', 'start_recovery']
        : context.isMediumOrHighConfidence
          ? ['step_by_step_explanation', 'concept_bridge', 'explain_simpler', 'one_more_like_this', 'advance', 'start_recovery']
          : ['explain_simpler', 'concept_bridge', 'step_by_step_explanation', 'one_more_like_this', 'advance', 'start_recovery'],
      uncertain_success: ['one_more_like_this', 'step_by_step_explanation', 'advance'],
      strong_momentum: ['advance', 'one_more_like_this']
    };
    if (context.explanationAlreadyUsed) {
      primaryPreferenceMap.repeated_weakness = ['start_recovery', 'one_more_like_this', 'advance', 'concept_bridge', 'step_by_step_explanation', 'explain_simpler'];
      primaryPreferenceMap.thoughtful_struggle = ['one_more_like_this', 'advance', 'concept_bridge', 'step_by_step_explanation', 'explain_simpler'];
      primaryPreferenceMap.confident_mistake = ['one_more_like_this', 'advance', 'concept_bridge', 'step_by_step_explanation', 'explain_simpler'];
      primaryPreferenceMap.uncertain_success = ['one_more_like_this', 'advance', 'step_by_step_explanation'];
    }
    var fallbackOrder = ['see_results', 'start_recovery', 'concept_bridge', 'step_by_step_explanation', 'explain_simpler', 'one_more_like_this', 'continue_normally', 'advance'];
    var primary = firstAvailableAction(actions, primaryPreferenceMap[stateKey] || []) || firstAvailableAction(actions, fallbackOrder);

    if (!primary) return null;

    var secondaryPriority = primary.id === 'start_recovery'
      ? ['concept_bridge', 'step_by_step_explanation', 'explain_simpler', 'continue_normally', 'one_more_like_this', 'advance']
      : primary.id === 'explain_simpler'
        ? ['concept_bridge', 'step_by_step_explanation', 'one_more_like_this', 'continue_normally', 'advance']
        : primary.id === 'step_by_step_explanation'
          ? ['concept_bridge', 'explain_simpler', 'one_more_like_this', 'continue_normally', 'advance']
          : primary.id === 'concept_bridge'
          ? ['step_by_step_explanation', 'explain_simpler', 'one_more_like_this', 'continue_normally', 'advance']
            : ['concept_bridge', 'step_by_step_explanation', 'explain_simpler', 'one_more_like_this', 'continue_normally', 'advance'];
    var forwardActionId = null;
    if (!context.sessionComplete && primary.id !== 'advance' && primary.id !== 'continue_normally') {
      if (context.recoveryStepAvailable && actions.continue_normally) {
        forwardActionId = 'continue_normally';
      } else if (actions.advance) {
        forwardActionId = 'advance';
      }
    }
    var secondaryIds = secondaryPriority.filter(function (id) {
      return id !== primary.id && id !== forwardActionId && !!actions[id];
    });
    if (forwardActionId) {
      secondaryIds = secondaryIds.slice(0, 2);
      secondaryIds.push(forwardActionId);
    } else {
      secondaryIds = secondaryIds.slice(0, 3);
    }
    var secondary = secondaryIds.map(function (id) { return actions[id]; });
    var copy = getNextStepCopy(stateKey, primary.id, context);
    var visualTone = getNextStepVisualTone(stateKey, primary, context);

    return {
      stateKey: stateKey,
      tone: visualTone,
      badge: copy.badge,
      title: copy.title,
      text: copy.text,
      primary: primary,
      secondary: secondary
    };
  }

  function renderNextStepPanel(nextStep) {
    var panel = $('#aq-next-step-panel');
    var labelEl = $('#aq-next-step-label');
    var titleEl = $('#aq-next-step-title');
    var textEl = $('#aq-next-step-text');
    var primaryWrap = $('#aq-next-step-primary');
    var secondaryWrap = $('#aq-next-step-secondary');
    var supportRow = $('#aq-support-row');
    var masteryRow = element.querySelector('.aq-mastery-row');
    if (!panel || !labelEl || !titleEl || !textEl || !primaryWrap || !secondaryWrap) return;

    if (!nextStep || !nextStep.primary) {
      hideNextStepPanel();
      if (supportRow) {
        supportRow.innerHTML = '';
        supportRow.classList.add('aq-hidden');
      }
      return;
    }

    labelEl.textContent = nextStep.badge;
    titleEl.textContent = nextStep.title;
    textEl.textContent = nextStep.text;
    removeNextStepToneClasses(panel);
    panel.setAttribute('data-tone', nextStep.tone || 'momentum');
    panel.classList.add('aq-next-step-' + (nextStep.tone || 'momentum'));
    panel.classList.remove('aq-hidden');
    if (masteryRow) {
      masteryRow.classList.toggle('aq-mastery-row-reinforcement', nextStep.tone === 'reinforcement');
    }

    primaryWrap.innerHTML = '';
    var primaryButton = createNextStepActionButton(nextStep.primary, 'primary');
    primaryWrap.appendChild(primaryButton);

    secondaryWrap.innerHTML = '';
    (nextStep.secondary || []).slice(0, 3).forEach(function (action) {
      secondaryWrap.appendChild(createNextStepActionButton(action, 'secondary'));
    });

    triggerNextStepMicroInteractions(panel, primaryButton);

    if (supportRow) {
      supportRow.innerHTML = '';
      supportRow.classList.add('aq-hidden');
    }
  }

  function refreshNextStepPanelFromCurrentFeedback() {
    if (!state.lastFeedbackContext || !state.lastFeedbackContext.data) return;

    var feedbackData = state.lastFeedbackContext.data;
    var recoveryOfferVisible = !!(
      feedbackData.recovery_step_available &&
      !feedbackData.session_complete &&
      !feedbackData.recovery_step_result
    );
    var nextStepContext = buildNextStepContext(
      feedbackData,
      state.lastFeedbackContext.selectedKey,
      recoveryOfferVisible
    );
    var availableActions = getAvailableNextStepActions(feedbackData, nextStepContext);
    var nextStep = chooseNextLearningAction(nextStepContext, availableActions);
    renderNextStepPanel(nextStep);

    var nextBtn = $('#aq-btn-next');
    if (nextBtn) {
      nextBtn.classList.toggle('aq-hidden', !!(nextStep && nextStep.primary) || recoveryOfferVisible);
    }
  }

  function renderFeedback(data, selectedKey, options) {
    options = options || {};

    if (!data || !data.success) {
      alert('Error: ' + (data && data.error ? data.error : 'Unknown'));
      return;
    }

    state.confidenceDismissMenuOpen = false;
    renderConfidenceUi();

    if (typeof data.questions_seen === 'number') state.questionsSeenSoFar = data.questions_seen;
    if (typeof data.session_score === 'number') state.sessionScore = data.session_score;
    if (data.max_questions) state.maxQuestionsCurrent = data.max_questions;

    state.lastTopic = (state.currentQuestion && state.currentQuestion.topic) || 'General';
    if (typeof data.updated_mastery === 'number') {
      state.lastMasteryPct = Math.round(data.updated_mastery * 100);
    }
    if (typeof data.next_difficulty === 'number') {
      state.lastDifficulty = data.next_difficulty;
    }

    var submittedAnswerMeta = cloneAnswerMetaForFeedback(state.lastAnswerMeta, selectedKey);
    if (data.recovery_step_result) {
      state.lastFeedbackContext = null;
    } else if (!options.isRestoredFeedback) {
      state.lastFeedbackContext = {
        data: cloneFeedbackContextData(data),
        selectedKey: selectedKey || null,
        answerMeta: submittedAnswerMeta,
        submittedConfidence: submittedAnswerMeta.confidence,
        confidenceProvided: submittedAnswerMeta.confidenceProvided,
        explanationAlreadyUsed: false,
        explanationFormats: createExplanationFormatsState()
      };
    }

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
    if (label) {
      label.textContent = data.recovery_step_result
        ? (data.is_correct ? 'Recovery step complete' : 'Recovery step checked')
        : (data.is_correct ? 'Correct!' : 'Incorrect');
    }

    var recoveryOfferVisible = !!(
      data.recovery_step_available &&
      !data.session_complete &&
      !data.recovery_step_result
    );

    renderStructuredExplanationForFeedback(data, selectedKey, recoveryOfferVisible, {
      textOverride: options.preserveExplanationText != null
        ? options.preserveExplanationText
        : null
    });
    hideExplainStatus();

    var bridgeWrap = $('#aq-narrative-bridge');
    var bridgeText = $('#aq-narrative-text');
    var bridgeLabel = $('#aq-narrative-label');

    if (bridgeWrap && bridgeText && bridgeLabel) {
      if (recoveryOfferVisible) {
        bridgeWrap.classList.add('aq-hidden');
      } else if (data.recovery_step_result) {
        bridgeLabel.textContent = 'Recovery step';
        bridgeText.textContent = data.recovery_result_message || 'We\'ll return to the normal quiz now.';
        bridgeWrap.classList.remove('aq-hidden');
      } else if (data.session_complete) {
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

    var pct = typeof data.updated_mastery === 'number'
      ? Math.round(data.updated_mastery * 100)
      : state.lastMasteryPct;
    var fillEl = $('#aq-mastery-fill');
    var pctEl = $('#aq-mastery-pct');
    if (fillEl) fillEl.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';

    var supportRow = $('#aq-support-row');
    if (supportRow) {
      supportRow.innerHTML = '';
      supportRow.classList.add('aq-hidden');
    }

    var nextBtn = $('#aq-btn-next');
    if (nextBtn) {
      nextBtn.textContent = data.recovery_step_result
        ? 'Continue Quiz →'
        : (data.session_complete ? 'See Results →' : 'Next Question →');
      nextBtn.onclick = data.recovery_step_result
        ? function () { loadNextQuestion(); }
        : data.session_complete
          ? function () { showResults(data); }
          : function () { loadNextQuestion(); };
    }

    var nextStepContext = buildNextStepContext(data, selectedKey, recoveryOfferVisible);
    var availableActions = getAvailableNextStepActions(data, nextStepContext);
    var nextStep = chooseNextLearningAction(nextStepContext, availableActions);
    renderNextStepPanel(nextStep);

    if (nextBtn) {
      nextBtn.classList.toggle('aq-hidden', !!(nextStep && nextStep.primary));
    }

    if (recoveryOfferVisible) {
      showRecoveryCard(data);
    } else {
      hideRecoveryCard();
    }

    if (nextBtn) {
      nextBtn.classList.toggle('aq-hidden', !!(nextStep && nextStep.primary) || recoveryOfferVisible);
    }

    var fb = $('#aq-feedback');
    if (fb) fb.classList.remove('aq-hidden');
  }

  function setExplanationText(text, options) {
    options = options || {};
    var expEl = $('#aq-explanation');
    if (!expEl) return;

    var feedbackData = state.lastFeedbackContext && state.lastFeedbackContext.data;
    var selectedKey = state.lastFeedbackContext && state.lastFeedbackContext.selectedKey;
    var recoveryOfferVisible = !!(
      feedbackData &&
      feedbackData.recovery_step_available &&
      !feedbackData.session_complete &&
      !feedbackData.recovery_step_result
    );

    if (feedbackData) {
      renderStructuredExplanationForFeedback(feedbackData, selectedKey, recoveryOfferVisible, {
        textOverride: text,
        modeOverride: options.modeOverride || null,
        forceExplanationAlreadyUsed: options.modeOverride === 'simple'
      });
      return;
    }

    stopExplanationReadAloud();
    expEl.classList.remove('aq-explanation-loading');
    expEl.setAttribute('data-explanation-body', text || '');
    expEl.setAttribute('data-explanation-mode', 'standard');
    expEl.setAttribute('data-explanation-takeaway', '');
    expEl.removeAttribute('data-explanation-intro');
    expEl.innerHTML =
      '<div class="aq-explanation-card aq-explanation-correct">' +
      '<div class="aq-explanation-card-header">' +
      '<span class="aq-explanation-label">Explanation</span>' +
      getReadAloudButtonMarkup() +
      '</div>' +
      '<h4 class="aq-explanation-title">Explanation</h4>' +
      '<p class="aq-explanation-body">' + escapeHtml(normalizeExplanationText(text)) + '</p>' +
      '</div>';
    updateReadAloudButtonState();
  }



  function setExplanationLoading(message) {
    var expEl = $('#aq-explanation');
    if (!expEl) return;
    stopExplanationReadAloud();
    expEl.classList.add('aq-explanation-loading');
    expEl.removeAttribute('data-explanation-body');
    expEl.removeAttribute('data-explanation-mode');
    expEl.removeAttribute('data-explanation-intro');
    expEl.removeAttribute('data-explanation-takeaway');
    expEl.innerHTML =
      '<span class="aq-inline-spinner" aria-hidden="true"></span>' +
      '<span>' + escapeHtml(message || 'Simplifying explanation…') + '</span>';
    updateReadAloudButtonState();
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

  function handleStartRecoveryStep() {
    if (state.recoveryStartPending) return;

    setRecoveryCardLoading(true, 'start');
    hideExplainStatus();

    jQuery.ajax({
      type: 'POST',
      url: urlStartRecovery,
      data: JSON.stringify({}),
      contentType: 'application/json',
      success: function (data) {
        if (!(data && data.success && data.worked_example_primer)) {
          setRecoveryCardLoading(false);
          showExplainStatus('Could not prepare the worked example just now.');
          return;
        }

        hideExplainStatus();
        openWorkedExampleScreen(data.worked_example_primer);
      },
      error: function () {
        setRecoveryCardLoading(false);
        showExplainStatus('Could not prepare the worked example just now.');
      }
    });
  }

  function handlePracticeRecoveryStep() {
    if (state.recoveryStartPending) return;

    setWorkedExampleActionLoading(true, 'practice');
    hideWorkedExampleStatus();

    jQuery.ajax({
      type: 'POST',
      url: urlPracticeRecovery,
      data: JSON.stringify({}),
      contentType: 'application/json',
      success: function (data) {
        if (!(data && data.success && data.question)) {
          setWorkedExampleActionLoading(false);
          showWorkedExampleStatus('Could not start the recovery practice just now.');
          return;
        }

        state.workedExamplePrimer = null;
        hideWorkedExampleStatus();
        setTimeout(function () {
          renderQuestion({
            success: true,
            question: data.question,
            questions_seen: state.questionsSeenSoFar,
            max_questions: state.maxQuestionsCurrent
          });
        }, 180);
      },
      error: function () {
        setWorkedExampleActionLoading(false);
        showWorkedExampleStatus('Could not start the recovery practice just now.');
      }
    });
  }

  function handleDeclineRecoveryStep() {
    if (state.recoveryStartPending) return;

    setRecoveryCardLoading(true, 'continue');
    jQuery.ajax({
      type: 'POST',
      url: urlDeclineRecovery,
      data: JSON.stringify({}),
      contentType: 'application/json',
      success: function (data) {
        setRecoveryCardLoading(false);
        if (!(data && data.success)) {
          showExplainStatus('Could not continue normally just now.');
          return;
        }
        restoreFeedbackAfterRecoveryDecline();
      },
      error: function () {
        setRecoveryCardLoading(false);
        showExplainStatus('Could not continue normally just now.');
      }
    });
  }

  function handleContinueFromWorkedExample() {
    if (state.recoveryStartPending) return;

    setWorkedExampleActionLoading(true, 'continue');
    hideWorkedExampleStatus();

    jQuery.ajax({
      type: 'POST',
      url: urlDeclineRecovery,
      data: JSON.stringify({}),
      contentType: 'application/json',
      success: function (data) {
        setWorkedExampleActionLoading(false);
        if (!(data && data.success)) {
          showWorkedExampleStatus('Could not continue to the quiz just now.');
          return;
        }
        state.workedExamplePrimer = null;
        restoreFeedbackAfterRecoveryDecline();
      },
      error: function () {
        setWorkedExampleActionLoading(false);
        showWorkedExampleStatus('Could not continue to the quiz just now.');
      }
    });
  }

  function setExplainSimplerButtonState(btn, isLoading) {
    if (!btn) return;
    var defaultLabel = btn.getAttribute('data-default-label') || 'Simpler explanation';
    btn.disabled = !!isLoading;
    btn.classList.toggle('aq-btn-support-disabled', !!isLoading);
    btn.textContent = isLoading ? 'Simplifying…' : defaultLabel;
    setAdaptiveActionButtonState('explain_simpler', !!isLoading, isLoading ? 'Simplifying…' : null);
  }

  function setStepByStepButtonState(btn, isLoading) {
    if (!btn) return;
    var defaultLabel = btn.getAttribute('data-default-label') || 'Work through step by step';
    btn.disabled = !!isLoading;
    btn.classList.toggle('aq-btn-support-disabled', !!isLoading);
    btn.textContent = isLoading ? 'Building steps…' : defaultLabel;
    setAdaptiveActionButtonState('step_by_step_explanation', !!isLoading, isLoading ? 'Building steps…' : null);
  }

  function setConceptBridgeButtonState(btn, isLoading) {
    if (!btn) return;
    var defaultLabel = btn.getAttribute('data-default-label') || 'Build concept bridge';
    btn.disabled = !!isLoading;
    btn.classList.toggle('aq-btn-support-disabled', !!isLoading);
    btn.textContent = isLoading ? 'Building bridge…' : defaultLabel;
    setAdaptiveActionButtonState('concept_bridge', !!isLoading, isLoading ? 'Building bridge…' : null);
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

  function formatMistakeCountLabel(count) {
    var total = parseInt(count, 10) || 0;
    return total + ' mistake' + (total === 1 ? '' : 's');
  }

  function getRecommendationData(sessionLike) {
    var title = String((sessionLike && sessionLike.recommendation_title) || '').trim();
    var text = String((sessionLike && sessionLike.recommendation_text) || (sessionLike && sessionLike.session_recommendation) || (sessionLike && sessionLike.recommendation) || '').trim();
    var code = String((sessionLike && sessionLike.recommendation_code) || '').trim();
    return {
      code: code,
      title: title,
      text: text
    };
  }

  function isPlaceholderLectureTitle(title) {
    var normalized = String(title || '').trim().toLowerCase();
    return !normalized || normalized === 'untitled content';
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

  function getSortedMasteryTopics(progressData, descending) {
    var mastery = (progressData && progressData.topic_mastery) || {};
    return Object.keys(mastery).sort(function (a, b) {
      var left = Number(mastery[a]);
      var right = Number(mastery[b]);
      if (!isFinite(left)) left = 0;
      if (!isFinite(right)) right = 0;
      return descending ? (right - left) : (left - right);
    });
  }

  function sortTopicsByMastery(topics, progressData, descending) {
    var mastery = (progressData && progressData.topic_mastery) || {};
    return normalizeTopicList(topics).slice().sort(function (a, b) {
      var left = Number(mastery[a]);
      var right = Number(mastery[b]);
      if (!isFinite(left)) left = descending ? 0 : 1;
      if (!isFinite(right)) right = descending ? 0 : 1;
      return descending ? (right - left) : (left - right);
    });
  }

  function deriveStrongTopics(progressData) {
    var mastery = (progressData && progressData.topic_mastery) || {};
    var topicLabels = (progressData && progressData.topic_labels) || {};
    var strongTopics = [];

    Object.keys(mastery).forEach(function (topic) {
      var score = Number(mastery[topic]);
      var label = String(topicLabels[topic] || '').toLowerCase();
      if (
        label === 'mastered' ||
        label === 'proficient' ||
        (isFinite(score) && score >= 0.70)
      ) {
        strongTopics.push(topic);
      }
    });

    return sortTopicsByMastery(strongTopics, progressData, true);
  }

  function deriveWeakTopics(progressData) {
    var mastery = (progressData && progressData.topic_mastery) || {};
    var topicLabels = (progressData && progressData.topic_labels) || {};
    var weakTopics = [];

    Object.keys(mastery).forEach(function (topic) {
      var score = Number(mastery[topic]);
      var label = String(topicLabels[topic] || '').toLowerCase();
      if (
        label === 'struggling' ||
        (isFinite(score) && score < 0.50)
      ) {
        weakTopics.push(topic);
      }
    });

    return sortTopicsByMastery(weakTopics, progressData, false);
  }

  function getDashboardTopicSignals(progressData) {
    var weakTopics = normalizeTopicList(progressData && progressData.weak_topics);
    var strongTopics = normalizeTopicList(progressData && progressData.strong_topics);

    return {
      weakTopics: weakTopics.length
        ? sortTopicsByMastery(weakTopics, progressData, false)
        : deriveWeakTopics(progressData),
      strongTopics: strongTopics.length
        ? sortTopicsByMastery(strongTopics, progressData, true)
        : deriveStrongTopics(progressData)
    };
  }

  function getStrongestCourseTopic(progressData, strongTopics) {
    var candidates = normalizeTopicList(strongTopics);
    if (candidates.length) return candidates[0];
    return getSortedMasteryTopics(progressData, true)[0] || '';
  }

  function getWeakestCourseTopic(progressData, weakTopics) {
    var candidates = normalizeTopicList(weakTopics);
    if (candidates.length) return candidates[0];
    return getSortedMasteryTopics(progressData, false)[0] || '';
  }

  function formatAccuracyPct(value) {
    return typeof value === 'number' ? (Math.round(value * 100) + '%') : '';
  }

  function formatCountLabel(count, singular, plural) {
    var total = parseInt(count, 10) || 0;
    return total + ' ' + (total === 1 ? singular : plural);
  }

  function joinDashboardMeta(parts, fallback) {
    var filtered = (parts || []).filter(function (part) {
      return !!String(part || '').trim();
    });
    return filtered.length ? filtered.join(' · ') : (fallback || '');
  }

  function hasMasteryTransition(summary) {
    if (!summary) return false;
    return isFinite(Number(summary.mastery_before)) && isFinite(Number(summary.mastery_after));
  }

  function formatMasteryTransition(summary) {
    if (!hasMasteryTransition(summary)) return '';
    var beforePct = masteryPct(summary.mastery_before);
    var afterPct = masteryPct(summary.mastery_after);
    return beforePct + '% → ' + afterPct + '%';
  }

  function buildFollowUpChangeSentence(summary) {
    if (!hasMasteryTransition(summary)) return '';
    var topic = String((summary && summary.topic) || 'The followed-up topic').trim();
    var beforePct = masteryPct(summary.mastery_before);
    var afterPct = masteryPct(summary.mastery_after);
    return topic + ' moved from ' + beforePct + '% to ' + afterPct + '% in the latest targeted review.';
  }

  function isFollowUpLikeSession(sessionLike) {
    return !!sessionLike && (
      isFollowUpSession(sessionLike) ||
      !!sessionLike.focused_topic_mastery_summary ||
      getFollowUpTopicMasterySummaries(sessionLike).length > 0
    );
  }

  function getLatestCourseSession(recentSessions) {
    if (!Array.isArray(recentSessions) || recentSessions.length === 0) return null;

    var sorted = recentSessions.slice().sort(function (a, b) {
      var left = new Date((a && (a.ended_at || a.started_at)) || 0).getTime();
      var right = new Date((b && (b.ended_at || b.started_at)) || 0).getTime();
      if (!isFinite(left)) left = 0;
      if (!isFinite(right)) right = 0;
      return right - left;
    });

    return sorted[0] || null;
  }

  function getRecentPerformanceSignals(recentSessions) {
    if (!Array.isArray(recentSessions) || recentSessions.length === 0) {
      return {
        recentAnswerCount: 0,
        recentCorrectCount: 0,
        recentIncorrectCount: 0,
        recentIncorrectRate: null
      };
    }

    var latestTwo = recentSessions.slice().sort(function (a, b) {
      var left = new Date((a && (a.ended_at || a.started_at)) || 0).getTime();
      var right = new Date((b && (b.ended_at || b.started_at)) || 0).getTime();
      if (!isFinite(left)) left = 0;
      if (!isFinite(right)) right = 0;
      return right - left;
    }).slice(0, 2);

    var totals = latestTwo.reduce(function (acc, session) {
      var answerCount = parseInt((session && session.target_questions), 10);
      var correctCount = parseInt((session && session.correct_answers), 10);

      if (!answerCount && session && Array.isArray(session.question_log) && session.question_log.length) {
        answerCount = session.question_log.length;
      }
      if (!isFinite(answerCount) || answerCount <= 0) return acc;

      if (!isFinite(correctCount) || correctCount < 0) correctCount = 0;
      if (correctCount > answerCount) correctCount = answerCount;

      acc.recentAnswerCount += answerCount;
      acc.recentCorrectCount += correctCount;
      acc.recentIncorrectCount += Math.max(answerCount - correctCount, 0);
      return acc;
    }, {
      recentAnswerCount: 0,
      recentCorrectCount: 0,
      recentIncorrectCount: 0,
      recentIncorrectRate: null
    });

    if (totals.recentAnswerCount > 0) {
      totals.recentIncorrectRate = totals.recentIncorrectCount / totals.recentAnswerCount;
    }

    return totals;
  }

  function formatRatePct(value) {
    return typeof value === 'number' ? (Math.round(value * 100) + '%') : '';
  }

  function buildFollowUpOutcome(model) {
    var latestSession = model && model.latestSession;
    if (!isFollowUpLikeSession(latestSession)) return null;

    var summary = model.primaryFollowUpSummary;
    var topicText = model.followUpTopicText || model.focusedTopicName || 'Targeted review';
    var recommendationText = model.recommendationData.text || '';
    var title = 'Latest follow-up outcome';
    var text = '';
    var meta = '';

    if (summary && String(summary.topic || '').trim()) {
      title = String(summary.topic).trim();
    } else if (topicText) {
      title = topicText;
    }

    if (summary && hasMasteryTransition(summary)) {
      text = formatMasteryTransition(summary);
      meta = recommendationText;
    } else if (recommendationText) {
      text = recommendationText;
    } else if (topicText) {
      text = topicText + ' shows the latest focused review signal for this course.';
    } else {
      text = 'The latest session included a focused follow-up signal.';
    }

    if (!meta && model.perspective === 'repair') {
      meta = 'Still needs reinforcement before returning to harder practice.';
    } else if (!meta && model.perspective === 'stretch') {
      meta = 'This improvement may support a return to harder practice.';
    } else if (!meta) {
      meta = 'Use this result alongside topic mastery and the latest session recommendation.';
    }

    return {
      label: 'Latest follow-up outcome',
      title: title,
      text: text,
      meta: meta
    };
  }

  function chooseDashboardPerspective(model) {
    var hasRepairSignal =
      model.latestRecommendationCode === 'focused_follow_up' ||
      (typeof model.overallAccuracy === 'number' && model.overallAccuracy < 0.55) ||
      (typeof model.recentIncorrectRate === 'number' && model.recentIncorrectRate >= 0.40) ||
      model.weakTopics.length >= 3 ||
      model.weakTopics.length > (model.strongTopics.length + 1);

    if (hasRepairSignal) return 'repair';

    var strongStretchFallback =
      typeof model.recentIncorrectRate !== 'number' &&
      typeof model.overallAccuracy === 'number' &&
      model.overallAccuracy >= 0.72 &&
      model.strongTopics.length >= 3 &&
      model.weakTopics.length <= 1;

    var stretchReady =
      typeof model.overallAccuracy === 'number' &&
      model.overallAccuracy >= 0.72 &&
      (typeof model.recentIncorrectRate === 'number'
        ? model.recentIncorrectRate <= 0.25
        : strongStretchFallback) &&
      model.strongTopics.length >= 2 &&
      model.weakTopics.length < model.strongTopics.length;

    if (stretchReady) return 'stretch';

    return 'growth';
  }

  function buildDashboardHero(model) {
    var hasTopicMastery = !!(model.progress && model.progress.topic_mastery && Object.keys(model.progress.topic_mastery).length);
    var hasSessionHistory = model.recentSessionsAvailable && model.recentSessions.length > 0;
    var primary = null;
    var secondary = null;
    var strongTopic = model.strongestTopic;
    var weakTopic = model.weakestTopic;

    if (model.perspective === 'repair') {
      primary = { label: 'Review mistake journal', action: { type: 'scroll', sectionId: 'aq-dashboard-section-mistake-journal' } };
      secondary = hasTopicMastery
        ? { label: 'Check topic mastery', action: { type: 'scroll', sectionId: 'aq-dashboard-section-topic-mastery' } }
        : (hasSessionHistory
          ? { label: 'Review latest sessions', action: { type: 'scroll', sectionId: 'aq-dashboard-section-session-history' } }
          : null);

      return {
        badge: 'Repair Focus',
        title: 'Here is the best next repair step',
        text: weakTopic
          ? 'Recent activity in this course shows recurring difficulty around ' + weakTopic + '. Review the mistake patterns first, then confirm the same area in topic mastery.'
          : 'Recent activity in this course shows recurring difficulty. Review the mistake patterns first, then confirm the same areas in topic mastery.',
        primary: primary,
        secondary: secondary
      };
    }

    if (model.perspective === 'stretch') {
      primary = { label: 'Start a new session', action: { type: 'new_session' } };
      secondary = hasTopicMastery
        ? { label: 'Review topic mastery', action: { type: 'scroll', sectionId: 'aq-dashboard-section-topic-mastery' } }
        : (hasSessionHistory
          ? { label: 'Review latest sessions', action: { type: 'scroll', sectionId: 'aq-dashboard-section-session-history' } }
          : null);

      return {
        badge: 'Stretch Potential',
        title: 'This course is showing strong enough progress for harder practice',
        text: 'Your recent course performance suggests that stronger practice is becoming appropriate. Challenge mode is still checked per selected lecture scope.',
        primary: primary,
        secondary: secondary
      };
    }

    primary = { label: 'Start a new session', action: { type: 'new_session' } };
    secondary = hasTopicMastery
      ? { label: 'View topic mastery', action: { type: 'scroll', sectionId: 'aq-dashboard-section-topic-mastery' } }
      : (hasSessionHistory
        ? { label: 'Review latest sessions', action: { type: 'scroll', sectionId: 'aq-dashboard-section-session-history' } }
        : null);

    return {
      badge: 'Growth Mode',
      title: 'Your progress is moving in the right direction',
      text: 'Recent sessions in this course show steady development. Keep building momentum and check which topics are improving.',
      primary: primary,
      secondary: secondary
    };
  }

  function buildDashboardFocusCard(model) {
    var accuracyText = formatAccuracyPct(model.overallAccuracy);
    var reviewTarget = model.recommendedReviewTopics[0] || model.weakestTopic;
    var recentIncorrectRateText = formatRatePct(model.recentIncorrectRate);
    var repairMeta = joinDashboardMeta([
      recentIncorrectRateText ? (recentIncorrectRateText + ' recent incorrect rate') : '',
      model.weakTopics.length
        ? formatCountLabel(model.weakTopics.length, 'weak topic', 'weak topics')
        : '',
      typeof model.recentAnswerCount === 'number' && model.recentAnswerCount > 0
        ? formatCountLabel(model.recentAnswerCount, 'recent answer', 'recent answers')
        : ''
    ], 'Review the weakest areas first.');

    if (model.perspective === 'repair') {
      return {
        label: 'Most urgent review area',
        title: reviewTarget || 'Course review priority',
        text: reviewTarget
          ? 'Recent course signals point back to ' + reviewTarget + ' as the clearest repair target. Recent performance suggests this area still needs reinforcement.'
          : 'Recent course signals show concentrated weakness. Recent performance suggests this course still needs focused repair.',
        meta: repairMeta
      };
    }

    if (model.perspective === 'stretch') {
      return {
        label: 'Strongest current area',
        title: model.strongestTopic || 'Course-level stretch readiness',
        text: model.strongestTopic
          ? model.strongestTopic + ' is one of the clearest strength signals in this course. Some other topics may still need development before a specific lecture scope unlocks challenge.'
          : 'Current course-level signals suggest some areas in this course are ready for more demanding practice.',
        meta: joinDashboardMeta([
          model.strongTopics.length
            ? formatCountLabel(model.strongTopics.length, 'strong topic', 'strong topics')
            : '',
          accuracyText ? (accuracyText + ' accuracy') : ''
        ], 'Review topic mastery for scope-specific readiness. Challenge mode is still checked per selected lecture scope.')
      };
    }

    return {
      label: 'Recent progress',
      title: model.strongestTopic ? ('Momentum in ' + model.strongestTopic) : 'Course momentum is building',
      text: model.strongestTopic
        ? 'Recent sessions suggest ' + model.strongestTopic + ' is becoming more reliable while the course continues to develop.'
        : 'This course shows healthy progress, but not yet enough evidence for repair or stretch.',
      meta: joinDashboardMeta([
        accuracyText ? (accuracyText + ' accuracy') : '',
        model.strongTopics.length
          ? formatCountLabel(model.strongTopics.length, 'strong topic', 'strong topics')
          : ''
      ], 'Keep building a broader base in this course.')
    };
  }

  function buildDashboardModel(progressData, recentSessions, mistakeGroups, sourceStatus) {
    sourceStatus = sourceStatus || {};

    var progress = progressData || {};
    var sessions = Array.isArray(recentSessions) ? recentSessions.slice() : [];
    var groups = Array.isArray(mistakeGroups)
      ? mistakeGroups.filter(function (group) {
        return !isPlaceholderLectureTitle(group && group.lecture_title);
      })
      : [];
    var recentSignals = getRecentPerformanceSignals(sessions);
    var topicSignals = getDashboardTopicSignals(progress);
    var latestSession = getLatestCourseSession(sessions);
    var followUpSummaries = latestSession ? getFollowUpTopicMasterySummaries(latestSession) : [];
    var focusedTopicSummary = latestSession && latestSession.focused_topic_mastery_summary
      ? latestSession.focused_topic_mastery_summary
      : null;
    var recommendationData = latestSession ? getRecommendationData(latestSession) : { code: '', title: '', text: '' };
    var recommendedReviewTopics = latestSession ? getRecommendedReviewTopics(latestSession) : [];
    var strongestTopic = getStrongestCourseTopic(progress, topicSignals.strongTopics) ||
      String((latestSession && latestSession.strongest_topic_this_session) || '').trim();
    var weakestTopic = getWeakestCourseTopic(progress, topicSignals.weakTopics) ||
      String((latestSession && latestSession.weakest_topic_this_session) || '').trim();
    var model = {
      courseId: String(progress.course_id || selectedCourseId || '').trim(),
      courseName: selectedCourseName || progress.course_id || '—',
      progress: progress,
      hasProgress: !!progress.has_progress,
      recentSessions: sessions,
      recentSessionsAvailable: !!sourceStatus.recentSessionsAvailable,
      mistakeGroups: groups,
      mistakeJournalAvailable: !!sourceStatus.mistakeJournalAvailable,
      latestSession: latestSession,
      overallAccuracy: typeof progress.overall_accuracy === 'number' ? progress.overall_accuracy : null,
      recentAnswerCount: recentSignals.recentAnswerCount,
      recentCorrectCount: recentSignals.recentCorrectCount,
      recentIncorrectCount: recentSignals.recentIncorrectCount,
      recentIncorrectRate: recentSignals.recentIncorrectRate,
      weakTopics: topicSignals.weakTopics,
      strongTopics: topicSignals.strongTopics,
      mistakeGroupCount: sourceStatus.mistakeJournalAvailable ? groups.length : null,
      recommendationData: recommendationData,
      latestRecommendationCode: recommendationData.code || '',
      recommendedReviewTopics: recommendedReviewTopics,
      followUpSummaries: followUpSummaries,
      primaryFollowUpSummary: focusedTopicSummary || followUpSummaries[0] || null,
      focusedTopicName: latestSession ? getFocusedTopicName(latestSession) : '',
      followUpTopicText: latestSession ? formatTopicList(getFollowUpPracticedTopics(latestSession)) : '',
      followUpContext: latestSession ? getFollowUpContext(latestSession) : null,
      hasFollowUpContext: isFollowUpLikeSession(latestSession),
      strongestTopic: strongestTopic,
      weakestTopic: weakestTopic
    };

    model.perspective = chooseDashboardPerspective(model);
    model.hero = buildDashboardHero(model);
    model.focusCard = buildDashboardFocusCard(model);
    model.followUpOutcome = buildFollowUpOutcome(model);

    return model;
  }

  function setDashboardPerspectiveClass(node, perspective) {
    if (!node) return;
    ['repair', 'stretch', 'growth'].forEach(function (name) {
      node.classList.remove('aq-dashboard-perspective-' + name);
    });
    node.classList.add('aq-dashboard-perspective-' + (perspective || 'growth'));
  }

  function startDashboardNewSession() {
    pickerMode = 'quiz';
    loadCoursePicker();
  }

  function scrollDashboardToSection(sectionId) {
    if (!sectionId) return;
    var selector = sectionId.charAt(0) === '#' ? sectionId : ('#' + sectionId);
    var target = $(selector);
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function runDashboardCta(action) {
    if (!action || !action.type) return;

    if (action.type === 'followup' && action.context) {
      startFocusedFollowUp(action.context);
      return;
    }
    if (action.type === 'scroll') {
      scrollDashboardToSection(action.sectionId);
      return;
    }
    if (action.type === 'new_session') {
      startDashboardNewSession();
    }
  }

  function renderDashboardAdaptiveHeader(model) {
    var heroWrap = $('#aq-dashboard-hero');
    var badgeEl = $('#aq-dashboard-hero-badge');
    var titleEl = $('#aq-dashboard-hero-title');
    var textEl = $('#aq-dashboard-hero-text');
    var primaryBtn = $('#aq-dashboard-hero-primary');
    var secondaryBtn = $('#aq-dashboard-hero-secondary');
    var focusWrap = $('#aq-dashboard-focus');
    var focusLabelEl = $('#aq-dashboard-focus-label');
    var focusTitleEl = $('#aq-dashboard-focus-title');
    var focusTextEl = $('#aq-dashboard-focus-text');
    var focusMetaEl = $('#aq-dashboard-focus-meta');
    var followUpWrap = $('#aq-dashboard-followup');
    var followUpLabelEl = $('#aq-dashboard-followup-label');
    var followUpTitleEl = $('#aq-dashboard-followup-title');
    var followUpTextEl = $('#aq-dashboard-followup-text');
    var followUpMetaEl = $('#aq-dashboard-followup-meta');
    var hero = model && model.hero ? model.hero : {};
    var focusCard = model && model.focusCard ? model.focusCard : {};
    var followUpOutcome = model && model.followUpOutcome ? model.followUpOutcome : null;
    var perspective = model && model.perspective ? model.perspective : 'growth';

    setDashboardPerspectiveClass(heroWrap, perspective);
    setDashboardPerspectiveClass(focusWrap, perspective);

    if (badgeEl) badgeEl.textContent = hero.badge || 'Growth Mode';
    if (titleEl) titleEl.textContent = hero.title || 'Your progress is moving in the right direction';
    if (textEl) {
      textEl.textContent = hero.text || 'Recent course activity will appear here with a focused recommendation for what to do next.';
    }

    if (primaryBtn) {
      primaryBtn.textContent = (hero.primary && hero.primary.label) || 'Start a new session';
      primaryBtn.onclick = function () {
        runDashboardCta(hero.primary && hero.primary.action);
      };
    }

    if (secondaryBtn) {
      if (hero.secondary && hero.secondary.label) {
        secondaryBtn.textContent = hero.secondary.label;
        secondaryBtn.onclick = function () {
          runDashboardCta(hero.secondary && hero.secondary.action);
        };
        secondaryBtn.classList.remove('aq-hidden');
      } else {
        secondaryBtn.onclick = null;
        secondaryBtn.classList.add('aq-hidden');
      }
    }

    if (focusLabelEl) focusLabelEl.textContent = focusCard.label || 'Recent progress';
    if (focusTitleEl) focusTitleEl.textContent = focusCard.title || 'Course-level progress overview';
    if (focusTextEl) {
      focusTextEl.textContent = focusCard.text || 'This view adapts to the currently selected course and updates as recent session and mistake data become available.';
    }
    if (focusMetaEl) {
      var focusMeta = focusCard.meta || '';
      focusMetaEl.textContent = focusMeta;
      focusMetaEl.classList.toggle('aq-hidden', !focusMeta);
    }

    if (followUpWrap) {
      if (followUpOutcome) {
        if (followUpLabelEl) followUpLabelEl.textContent = followUpOutcome.label || 'Latest follow-up outcome';
        if (followUpTitleEl) followUpTitleEl.textContent = followUpOutcome.title || 'Targeted review';
        if (followUpTextEl) followUpTextEl.textContent = followUpOutcome.text || 'The latest session included a follow-up signal.';
        if (followUpMetaEl) {
          var followUpMeta = followUpOutcome.meta || '';
          followUpMetaEl.textContent = followUpMeta;
          followUpMetaEl.classList.toggle('aq-hidden', !followUpMeta);
        }
        followUpWrap.classList.remove('aq-hidden');
      } else {
        followUpWrap.classList.add('aq-hidden');
      }
    }
  }

  function shouldReduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function triggerDashboardTopCardEntrance() {
    var cards = [$('#aq-dashboard-hero'), $('#aq-dashboard-focus')].filter(function (card) {
      return !!card;
    });
    if (!cards.length) return;

    cards.forEach(function (card) {
      card.classList.remove('aq-dashboard-card-enter');
      card.style.animationDelay = '0ms';
    });

    if (shouldReduceMotion()) return;

    void cards[0].offsetWidth;
    cards.forEach(function (card, index) {
      card.style.animationDelay = (index * 50) + 'ms';
      card.classList.add('aq-dashboard-card-enter');
    });
  }

  function triggerDashboardMasteryBars() {
    var topicsWrap = $('#aq-dashboard-topics');
    if (!topicsWrap) return;

    var fills = Array.prototype.slice.call(topicsWrap.querySelectorAll('.aq-dash-fill'));
    if (!fills.length) return;

    fills.forEach(function (fill) {
      var targetWidth = fill.style.width || fill.getAttribute('data-target-width') || '0%';
      fill.setAttribute('data-target-width', targetWidth);
      fill.classList.remove('aq-dash-fill-animate');
      fill.classList.remove('aq-dash-fill-prep');
      fill.style.transitionDelay = '0ms';
      fill.style.width = targetWidth;
    });

    if (shouldReduceMotion()) return;

    fills.forEach(function (fill) {
      fill.classList.add('aq-dash-fill-prep');
      fill.style.width = '0%';
    });

    var scheduleFrame = window.requestAnimationFrame || function (callback) {
      return window.setTimeout(callback, 16);
    };
    scheduleFrame(function () {
      scheduleFrame(function () {
        fills.forEach(function (fill, index) {
          fill.classList.remove('aq-dash-fill-prep');
          fill.classList.add('aq-dash-fill-animate');
          fill.style.transitionDelay = Math.min(index * 35, 315) + 'ms';
          fill.style.width = fill.getAttribute('data-target-width') || '0%';
        });
      });
    });
  }

  function scheduleDashboardMicroInteractions(options, attempt) {
    options = options || {};
    attempt = attempt || 0;

    if (shouldReduceMotion()) return;

    var dashboardScreen = $('#aq-screen-dashboard');

    if (!dashboardScreen || dashboardScreen.classList.contains('aq-hidden')) {
      if (attempt < 1) {
        window.setTimeout(function () {
          scheduleDashboardMicroInteractions(options, attempt + 1);
        }, 90);
      }
      return;
    }

    var scheduleFrame = window.requestAnimationFrame || function (callback) {
      return window.setTimeout(callback, 16);
    };

    scheduleFrame(function () {
      scheduleFrame(function () {
        window.setTimeout(function () {
          var visibleDashboard = $('#aq-screen-dashboard');
          if (!visibleDashboard || visibleDashboard.classList.contains('aq-hidden')) return;

          if (options.cards) {
            triggerDashboardTopCardEntrance();
          }

          if (options.mastery) {
            triggerDashboardMasteryBars();
          }
        }, 90);
      });
    });
  }

  function applyDashboardSectionOrder(perspective) {
    var panel = $('#aq-screen-dashboard .aq-panel');
    var actions = $('#aq-screen-dashboard .aq-panel-actions');
    var orderMap = {
      repair: [
        'aq-dashboard-section-mistake-journal',
        'aq-dashboard-section-topic-mastery',
        'aq-dashboard-section-session-history'
      ],
      growth: [
        'aq-dashboard-section-topic-mastery',
        'aq-dashboard-section-mistake-journal',
        'aq-dashboard-section-session-history'
      ],
      stretch: [
        'aq-dashboard-section-topic-mastery',
        'aq-dashboard-section-mistake-journal',
        'aq-dashboard-section-session-history'
      ]
    };
    var order = orderMap[perspective] || orderMap.growth;

    if (!panel || !actions) return;

    order.forEach(function (id) {
      var section = $('#' + id);
      if (section) panel.insertBefore(section, actions);
    });
  }

  function loadAdaptiveDashboardData(courseId) {
    return {
      progress: jQuery.ajax({
        type: 'POST',
        url: urlProgress,
        data: JSON.stringify({ selected_course_id: courseId }),
        contentType: 'application/json'
      }),
      recentSessions: loadSessionHistory(courseId, {
        skipRender: true,
        limit: 2,
        timeout: 8000
      }),
      mistakeGroups: loadMistakeJournal(courseId, {
        skipRender: true,
        timeout: 8000
      })
    };
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

  function getReviewItems() {
    return Array.isArray(reviewState.items) ? reviewState.items : [];
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

    var originalExplanation = getExplanationTextFromEl();
    var formats = getExplanationFormatsState();

    if (formats.simplerText) {
      formats.simplerUsed = true;
      syncExplanationUsedUmbrella();
      setExplanationText(formats.simplerText, { modeOverride: 'simple' });
      hideExplainStatus();
      refreshNextStepPanelFromCurrentFeedback();
      return;
    }

    state.explainSimplerPending = true;
    hideExplainStatus();
    setExplainSimplerButtonState(btn, true);
    setExplanationLoading('Simplifying explanation…');

    jQuery.ajax({
      type: 'POST', url: urlExplain,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        state.explainSimplerPending = false;
        setExplainSimplerButtonState(btn, false);

        if (data && data.success) {
          if (state.lastFeedbackContext) {
            var latestFormats = getExplanationFormatsState();
            latestFormats.simplerUsed = true;
            latestFormats.simplerText = data.simpler_explanation || '';
            syncExplanationUsedUmbrella();
          }
          setExplanationText(data.simpler_explanation, { modeOverride: 'simple' });
          hideExplainStatus();
          refreshNextStepPanelFromCurrentFeedback();
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

  function handleStepByStepExplanation(btn) {
    if (state.stepByStepExplanationPending) return;

    var originalExplanation = getExplanationTextFromEl();
    var formats = getExplanationFormatsState();

    if (formats.stepByStepData) {
      formats.stepByStepUsed = true;
      syncExplanationUsedUmbrella();
      renderStepByStepExplanation(formats.stepByStepData);
      hideExplainStatus();
      refreshNextStepPanelFromCurrentFeedback();
      return;
    }

    state.stepByStepExplanationPending = true;
    hideExplainStatus();
    setStepByStepButtonState(btn, true);
    setExplanationLoading('Building step-by-step explanation…');

    jQuery.ajax({
      type: 'POST',
      url: urlStepByStep,
      data: JSON.stringify({
        selected_answer: state.lastFeedbackContext && state.lastFeedbackContext.selectedKey
      }),
      contentType: 'application/json',
      success: function (data) {
        state.stepByStepExplanationPending = false;
        setStepByStepButtonState(btn, false);

        if (data && data.success && data.step_by_step_explanation) {
          if (state.lastFeedbackContext) {
            var latestFormats = getExplanationFormatsState();
            latestFormats.stepByStepUsed = true;
            latestFormats.stepByStepData = data.step_by_step_explanation;
            syncExplanationUsedUmbrella();
          }
          renderStepByStepExplanation(data.step_by_step_explanation);
          hideExplainStatus();
          refreshNextStepPanelFromCurrentFeedback();
          return;
        }

        setExplanationText(originalExplanation);
        showExplainStatus('Could not build the step-by-step explanation just now.');
      },
      error: function () {
        state.stepByStepExplanationPending = false;
        setStepByStepButtonState(btn, false);
        setExplanationText(originalExplanation);
        showExplainStatus('Could not build the step-by-step explanation just now.');
      }
    });
  }

  function handleConceptBridge(btn) {
    if (state.conceptBridgePending) return;
    if (!state.lastFeedbackContext || !state.lastFeedbackContext.data) return;

    var originalExplanation = getExplanationTextFromEl();
    var formats = getExplanationFormatsState();
    var candidate = formats.conceptBridgeCandidate || getConceptBridgeCandidate({
      topic: state.currentQuestion && state.currentQuestion.topic
    });

    if (!candidate || !candidate.available) {
      showExplainStatus('Could not find an earlier concept to bridge from this question.');
      refreshNextStepPanelFromCurrentFeedback();
      return;
    }

    formats.conceptBridgeCandidate = candidate;

    if (formats.conceptBridgeData) {
      formats.conceptBridgeUsed = true;
      syncExplanationUsedUmbrella();
      renderConceptBridge(formats.conceptBridgeData);
      hideExplainStatus();
      refreshNextStepPanelFromCurrentFeedback();
      return;
    }

    state.conceptBridgePending = true;
    hideExplainStatus();
    setConceptBridgeButtonState(btn, true);
    setExplanationLoading('Building concept bridge…');

    jQuery.ajax({
      type: 'POST',
      url: urlConceptBridge,
      data: JSON.stringify({
        candidate: candidate,
        selected_answer: state.lastFeedbackContext.selectedKey,
        selected_content_ids: selectedContentIds.slice()
      }),
      contentType: 'application/json',
      success: function (data) {
        state.conceptBridgePending = false;
        setConceptBridgeButtonState(btn, false);

        if (data && data.success && data.concept_bridge) {
          if (state.lastFeedbackContext) {
            var latestFormats = getExplanationFormatsState();
            latestFormats.conceptBridgeUsed = true;
            latestFormats.conceptBridgeData = data.concept_bridge;
            latestFormats.conceptBridgeCandidate = candidate;
            syncExplanationUsedUmbrella();
          }
          renderConceptBridge(data.concept_bridge);
          hideExplainStatus();
          refreshNextStepPanelFromCurrentFeedback();
          return;
        }

        setExplanationText(originalExplanation);
        showExplainStatus('Could not build the concept bridge just now.');
      },
      error: function () {
        state.conceptBridgePending = false;
        setConceptBridgeButtonState(btn, false);
        setExplanationText(originalExplanation);
        showExplainStatus('Could not build the concept bridge just now.');
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
    var recommendationData = getRecommendationData(data);
    var recommendation = recommendationData.text;
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
    var recTitleEl = $('#aq-recommendation-title');
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
    if (recTitleEl) {
      recTitleEl.textContent = recommendationData.title || 'Recommended next step';
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

    var recommendationData = getRecommendationData(data);
    var followUp = getFollowUpContext(data);
    if (!followUp || recommendationData.code !== 'focused_follow_up') {
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

    if (!data.has_progress || !data.topic_mastery || Object.keys(data.topic_mastery).length === 0) {
      if (emptyEl) emptyEl.classList.remove('aq-hidden');
      showScreen('dashboard');
      return;
    }
    if (emptyEl) emptyEl.classList.add('aq-hidden');

    if (topicsWrap) {
      var mastery = data.topic_mastery || {};
      var topicLabels = data.topic_labels || {};
      var contentItems = data.content_items || [];

      renderGroupedMastery(topicsWrap, mastery, topicLabels, contentItems);
    }

    showScreen('dashboard');
    scheduleDashboardMicroInteractions({
      mastery: true,
      cards: false
    });
  }

  function renderMistakeJournal(groups) {
    var wrap = $('#aq-mistake-journal-groups');
    var empty = $('#aq-mistake-journal-empty');

    if (!wrap) return;
    wrap.innerHTML = '';
    wrap.classList.add('aq-mistake-groups');

    var renderedGroups = (groups || []).filter(function (group) {
      return !isPlaceholderLectureTitle(group && group.lecture_title);
    });

    if (!renderedGroups.length) {
      if (empty) empty.classList.remove('aq-hidden');
      return;
    }

    if (empty) empty.classList.add('aq-hidden');

    var totalLectures = renderedGroups.length;
    var totalTopics = renderedGroups.reduce(function (sum, group) {
      return sum + (parseInt(group.topic_count, 10) || 0);
    }, 0);
    var totalMistakes = renderedGroups.reduce(function (sum, group) {
      return sum + (parseInt(group.mistake_count, 10) || 0);
    }, 0);

    var lecturesDetails = document.createElement('details');
    lecturesDetails.className = 'aq-accordion aq-accordion-type aq-mistake-outer-group';
    lecturesDetails.open = true;
    lecturesDetails.innerHTML =
      '<summary class="aq-accordion-summary">' +
      '<div class="aq-accordion-summary-main">' +
      '<span class="aq-accordion-title">Lectures</span>' +
      '<span class="aq-accordion-meta">' +
      totalLectures + ' lecture' + (totalLectures === 1 ? '' : 's') +
      ' · ' +
      totalTopics + ' topic' + (totalTopics === 1 ? '' : 's') +
      '</span>' +
      '</div>' +
      '<div class="aq-accordion-summary-side">' +
      '<span class="aq-accordion-score">' + formatMistakeCountLabel(totalMistakes) + '</span>' +
      '<span class="aq-accordion-chevron" aria-hidden="true">⌄</span>' +
      '</div>' +
      '</summary>';

    var lecturesBody = document.createElement('div');
    lecturesBody.className = 'aq-accordion-body aq-mistake-outer-body';

    renderedGroups.forEach(function (group, groupIndex) {
      var details = document.createElement('details');
      details.className = 'aq-accordion aq-accordion-item aq-mistake-group';
      if (groupIndex === 0) details.open = true;

      var metaParts = [];
      if (typeof group.lecture_week === 'number') {
        metaParts.push('Week ' + group.lecture_week);
      }
      if (group.lecture_scope_kind === 'scope') {
        metaParts.push('Selected content scope');
      }
      metaParts.push((group.topic_count || 0) + ' topic' + ((group.topic_count || 0) === 1 ? '' : 's') + ' affected');

      var summary = document.createElement('summary');
      summary.className = 'aq-accordion-summary';
      summary.innerHTML =
        '<div class="aq-accordion-summary-main">' +
        '<div class="aq-accordion-title aq-accordion-title-item">' + escapeHtml(group.lecture_title || 'Selected content') + '</div>' +
        '<div class="aq-accordion-meta">' + escapeHtml(metaParts.join(' · ')) + '</div>' +
        '</div>' +
        '<div class="aq-accordion-summary-side">' +
        '<span class="aq-accordion-score">' + formatMistakeCountLabel(group.mistake_count || 0) + '</span>' +
        '<span class="aq-accordion-chevron" aria-hidden="true">⌄</span>' +
        '</div>';
      details.appendChild(summary);

      var body = document.createElement('div');
      body.className = 'aq-accordion-body aq-accordion-body-item aq-mistake-topic-list';

      (group.topics || []).forEach(function (topicGroup) {
        var row = document.createElement('div');
        row.className = 'aq-mistake-topic-row';
        row.innerHTML =
          '<div class="aq-mistake-topic-main">' +
          '<div class="aq-mistake-topic-name">' + escapeHtml(topicGroup.topic || 'General') + '</div>' +
          '<div class="aq-mistake-topic-meta">' +
          '<span class="aq-mistake-topic-count">' + formatMistakeCountLabel(topicGroup.mistake_count || 0) + '</span>' +
          '<span>Latest: ' + escapeHtml(formatDateTime(topicGroup.latest_at)) + '</span>' +
          '</div>' +
          '</div>' +
          '<button class="aq-btn-session aq-btn-mistake-review" type="button" data-lecture-key="' + escapeHtml(group.lecture_key || '') + '" data-topic="' + escapeHtml(topicGroup.topic || '') + '">Review mistakes</button>';

        body.appendChild(row);
      });

      details.appendChild(body);
      lecturesBody.appendChild(details);
    });

    lecturesDetails.appendChild(lecturesBody);
    wrap.appendChild(lecturesDetails);

    wrap.querySelectorAll('.aq-btn-mistake-review').forEach(function (btn) {
      btn.addEventListener('click', function () {
        loadMistakeReview(
          btn.getAttribute('data-lecture-key'),
          btn.getAttribute('data-topic'),
          btn
        );
      });
    });
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
      if (session && session.session_id && session.question_log && session.question_log.length) {
        state.sessionReviewCache[session.session_id] = session;
      }

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
      var recommendationData = getRecommendationData(session);
      var recommendationHeading = recommendationData.title || 'Recommendation';
      var recommendationText = recommendationData.text || 'Keep building mastery through regular practice.';

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
        '<strong>' + escapeHtml(recommendationHeading) + ':</strong> ' + escapeHtml(recommendationText) +
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

  function formatRecoveryTriggerReason(reason) {
    if (reason === 'thoughtful_wrong_answer') return 'thoughtful struggle';
    if (reason === 'repeated_wrong_topic') return 'repeated difficulty';
    return 'guided support';
  }

  function formatRecoveryOutcome(question) {
    if (!question) return 'still needs review';
    if (question.recovery_outcome === 'recovered') return 'recovered';
    if (question.recovery_outcome === 'still_needs_review') return 'still needs review';
    return question.is_correct ? 'recovered' : 'still needs review';
  }

  function formatRecoveryContextOutcome(recoveryContext) {
    if (!recoveryContext) return 'still needs review';
    if (recoveryContext.recovery_outcome === 'recovered') return 'recovered';
    if (recoveryContext.recovery_outcome === 'still_needs_review') return 'still needs review';
    if (recoveryContext.guided_recovery_used) return 'guided recovery used';
    if (recoveryContext.worked_example_primer_used) {
      if (recoveryContext.worked_example_primer_choice === 'continue_to_quiz') return 'worked example used';
      if (recoveryContext.worked_example_primer_choice === 'practice_one_yourself') return 'worked example then practice';
      return 'worked example viewed';
    }
    if (recoveryContext.guided_recovery_offered) return 'guided recovery offered';
    return 'still needs review';
  }

  function formatConfidenceLabel(confidence) {
    var normalized = String(confidence || '').trim().toLowerCase();
    if (normalized === 'low') return 'Not sure';
    if (normalized === 'medium') return 'Somewhat sure';
    if (normalized === 'high') return 'Very sure';
    return '';
  }

  function openReviewModal(config) {
    reviewState.items = Array.isArray(config && config.items) ? config.items.slice() : [];
    reviewState.questionIndex = 0;
    reviewState.mode = (config && config.mode) || 'session';
    reviewState.badge = (config && config.badge) || 'Session Review';
    reviewState.title = (config && config.title) || 'Session Review';
    reviewState.subtitle = (config && config.subtitle) || '—';

    renderReviewQuestion();

    $('#aq-review-modal').classList.remove('aq-hidden');
    document.body.classList.add('aq-modal-open');
  }

  function renderReviewQuestion() {
    var items = getReviewItems();
    if (!items.length) return;

    var q = items[reviewState.questionIndex];
    var total = items.length;

    var badgeEl = $('#aq-review-badge');
    if (badgeEl) badgeEl.textContent = reviewState.badge || 'Session Review';
    $('#aq-review-session-title').textContent = reviewState.title || 'Session Review';
    $('#aq-review-session-sub').textContent = reviewState.subtitle || '—';

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
    var confidenceChip = $('#aq-review-confidence-chip');
    if (confidenceChip) {
      var confidenceLabel = formatConfidenceLabel(q.confidence);
      confidenceChip.textContent = confidenceLabel ? ('Confidence: ' + confidenceLabel) : 'Confidence: —';
      confidenceChip.classList.toggle('aq-hidden', !confidenceLabel);
    }
    var sourceChip = $('#aq-review-source-chip');
    if (sourceChip) {
      var sourceText = String(q.session_reference || '').trim();
      sourceChip.textContent = sourceText || 'Session';
      sourceChip.classList.toggle('aq-hidden', !(reviewState.mode === 'mistake' && sourceText));
    }
    var dateChip = $('#aq-review-date-chip');
    if (dateChip) {
      var loggedAt = q.session_ended_at ? ('Logged: ' + formatDateTime(q.session_ended_at)) : '';
      dateChip.textContent = loggedAt || 'Logged: —';
      dateChip.classList.toggle('aq-hidden', !(reviewState.mode === 'mistake' && loggedAt));
    }
    var recoveryChip = $('#aq-review-recovery-chip');
    if (recoveryChip) {
      recoveryChip.classList.add('aq-session-meta-chip-recovery');
      if (q.is_recovery_step) {
        recoveryChip.textContent = 'Guided Recovery';
        recoveryChip.classList.remove('aq-hidden');
      } else if (q.recovery_context && q.recovery_context.guided_recovery_used) {
        recoveryChip.textContent = 'Guided Recovery Used';
        recoveryChip.classList.remove('aq-hidden');
      } else if (q.recovery_context && q.recovery_context.worked_example_primer_used) {
        recoveryChip.textContent = 'Worked Example Used';
        recoveryChip.classList.remove('aq-hidden');
      } else {
        recoveryChip.classList.add('aq-hidden');
      }
    }
    var recoveryMeta = $('#aq-review-recovery-meta');
    var recoveryTrigger = $('#aq-review-recovery-trigger');
    var recoveryTopic = $('#aq-review-recovery-topic');
    var recoveryOutcome = $('#aq-review-recovery-outcome');
    if (recoveryMeta && recoveryTrigger && recoveryTopic && recoveryOutcome) {
      if (q.is_recovery_step) {
        recoveryTrigger.textContent = formatRecoveryTriggerReason(q.recovery_trigger_reason);
        recoveryTopic.textContent = q.recovery_for_topic || q.topic || 'General';
        recoveryOutcome.textContent = formatRecoveryOutcome(q);
        recoveryMeta.classList.remove('aq-hidden');
      } else if (q.recovery_context && (q.recovery_context.guided_recovery_used || q.recovery_context.guided_recovery_offered)) {
        recoveryTrigger.textContent = formatRecoveryTriggerReason(q.recovery_context.trigger_reason);
        recoveryTopic.textContent = q.topic || 'General';
        recoveryOutcome.textContent = formatRecoveryContextOutcome(q.recovery_context);
        recoveryMeta.classList.remove('aq-hidden');
      } else {
        recoveryMeta.classList.add('aq-hidden');
      }
    }
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
    if (!session || !session.question_log || !session.question_log.length) return;

    openReviewModal({
      mode: 'session',
      badge: 'Session Review',
      title: 'Session Review',
      subtitle:
        formatDateTime(session.ended_at || session.started_at) +
        ' · Score: ' + (session.correct_answers || 0) + '/' + (session.target_questions || 0),
      items: session.question_log
    });
  }

  function openMistakeReview(reviewData) {
    if (!reviewData || !reviewData.entries || !reviewData.entries.length) return;

    var lectureTitle = reviewData.lecture && reviewData.lecture.lecture_title
      ? reviewData.lecture.lecture_title
      : 'Selected content';
    var topic = reviewData.topic || 'General';
    var mistakeCount = reviewData.mistake_count || reviewData.entries.length;

    openReviewModal({
      mode: 'mistake',
      badge: 'Mistake Journal',
      title: 'Mistake Review',
      subtitle:
        'Lecture: ' + lectureTitle +
        ' · Topic: ' + topic +
        ' · ' + formatMistakeCountLabel(mistakeCount),
      items: reviewData.entries
    });
  }

  function closeSessionReview() {
    $('#aq-review-modal').classList.add('aq-hidden');
    document.body.classList.remove('aq-modal-open');
    reviewState.items = [];
    reviewState.questionIndex = 0;
    reviewState.mode = 'session';
    reviewState.badge = 'Session Review';
    reviewState.title = 'Session Review';
    reviewState.subtitle = '—';
  }

  function openSessionReviewById(sessionId, triggerBtn) {
    if (!sessionId) return;

    if (state.sessionReviewCache[sessionId]) {
      openSessionReview(state.sessionReviewCache[sessionId]);
      return;
    }

    var originalLabel = triggerBtn ? triggerBtn.textContent : '';
    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.textContent = 'Loading Review…';
    }

    jQuery.ajax({
      type: 'POST',
      url: urlSessionDetail,
      data: JSON.stringify({
        selected_course_id: selectedCourseId,
        session_id: sessionId
      }),
      contentType: 'application/json',
      success: function (data) {
        if (triggerBtn) {
          triggerBtn.disabled = false;
          triggerBtn.textContent = originalLabel || 'Review Session';
        }

        var session = data && data.success ? data.session : null;
        if (!session || !session.question_log || !session.question_log.length) {
          alert('Could not load session review.');
          return;
        }

        state.sessionReviewCache[sessionId] = session;
        openSessionReview(session);
      },
      error: function () {
        if (triggerBtn) {
          triggerBtn.disabled = false;
          triggerBtn.textContent = originalLabel || 'Review Session';
        }
        alert('Could not load session review.');
      }
    });
  }

  function loadSessionHistory(courseId) {
    var options = arguments[1] || {};
    return jQuery.ajax({
      type: 'POST',
      url: urlSessionHistory,
      data: JSON.stringify({
        selected_course_id: courseId,
        limit: options.limit || 1,
        include_questions: false
      }),
      contentType: 'application/json',
      timeout: options.timeout || 0
    }).then(function (data) {
      var sessions = (data && data.success) ? (data.sessions || []) : [];
      if (!options.skipRender) {
        renderSessionHistory(sessions, {
          wrap: $('#aq-session-history-list'),
          empty: $('#aq-session-history-empty'),
          showReviewButton: false,
          allowFollowUp: false
        });
      }
      return {
        available: !!(data && data.success),
        sessions: sessions
      };
    }, function () {
      if (!options.skipRender) {
        renderSessionHistory([], {
          wrap: $('#aq-session-history-list'),
          empty: $('#aq-session-history-empty'),
          showReviewButton: false,
          allowFollowUp: false
        });
      }
      return {
        available: false,
        sessions: []
      };
    });
  }

  function loadMistakeJournal(courseId) {
    var options = arguments[1] || {};
    return jQuery.ajax({
      type: 'POST',
      url: urlMistakeJournal,
      data: JSON.stringify({
        selected_course_id: courseId
      }),
      contentType: 'application/json',
      timeout: options.timeout || 0
    }).then(function (data) {
      var groups = (data && data.success) ? (data.groups || []) : [];
      if (!options.skipRender) {
        renderMistakeJournal(groups);
      }
      return {
        available: !!(data && data.success),
        groups: groups
      };
    }, function () {
      if (!options.skipRender) {
        renderMistakeJournal([]);
      }
      return {
        available: false,
        groups: []
      };
    });
  }

  function loadMistakeReview(lectureKey, topic, triggerBtn) {
    var originalLabel = triggerBtn ? triggerBtn.textContent : '';
    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.textContent = 'Loading…';
    }

    jQuery.ajax({
      type: 'POST',
      url: urlMistakeReview,
      data: JSON.stringify({
        selected_course_id: selectedCourseId,
        lecture_key: lectureKey,
        topic: topic
      }),
      contentType: 'application/json',
      success: function (data) {
        if (triggerBtn) {
          triggerBtn.disabled = false;
          triggerBtn.textContent = originalLabel || 'Review mistakes';
        }

        if (!data || !data.success || !data.entries || !data.entries.length) {
          alert('Could not load mistake review.');
          return;
        }

        openMistakeReview(data);
      },
      error: function () {
        if (triggerBtn) {
          triggerBtn.disabled = false;
          triggerBtn.textContent = originalLabel || 'Review mistakes';
        }
        alert('Could not load mistake review.');
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
    state.dashboardLoadToken += 1;
    state.dashboardModel = null;

    var loadToken = state.dashboardLoadToken;
    var adaptiveData = {
      progress: null,
      recentSessions: [],
      recentSessionsAvailable: false,
      mistakeGroups: [],
      mistakeJournalAvailable: false
    };
    var dashboardTopCardsAnimated = false;

    function isStaleDashboardLoad() {
      return loadToken !== state.dashboardLoadToken;
    }

    function renderAdaptiveDashboard() {
      if (isStaleDashboardLoad() || !adaptiveData.progress) return;

      var model = buildDashboardModel(
        adaptiveData.progress,
        adaptiveData.recentSessions,
        adaptiveData.mistakeGroups,
        {
          recentSessionsAvailable: adaptiveData.recentSessionsAvailable,
          mistakeJournalAvailable: adaptiveData.mistakeJournalAvailable
        }
      );

      state.dashboardModel = model;
      renderDashboardAdaptiveHeader(model);
      applyDashboardSectionOrder(model.perspective);
      if (!dashboardTopCardsAnimated) {
        scheduleDashboardMicroInteractions({
          cards: true,
          mastery: false
        });
        dashboardTopCardsAnimated = true;
      }
    }

    renderSessionHistory([], {
      wrap: $('#aq-session-history-list'),
      empty: $('#aq-session-history-empty'),
      showReviewButton: false,
      allowFollowUp: false
    });
    renderMistakeJournal([]);

    setLoading('Loading your progress…');
    var requests = loadAdaptiveDashboardData(selectedCourseId);

    requests.progress.done(function (data) {
      if (isStaleDashboardLoad()) return;

      if (!data || !data.success) {
        state.dashboardLoadToken += 1;
        alert('Dashboard error: ' + ((data && data.error) ? data.error : 'Unknown'));
        showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
        return;
      }

      adaptiveData.progress = data;
      renderDashboard(data);
      renderAdaptiveDashboard();
    }).fail(function (xhr) {
      if (isStaleDashboardLoad()) return;
      state.dashboardLoadToken += 1;
      alert('Could not load progress. HTTP ' + xhr.status);
      showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
    });

    requests.recentSessions.done(function (result) {
      if (isStaleDashboardLoad()) return;
      adaptiveData.recentSessions = result.sessions || [];
      adaptiveData.recentSessionsAvailable = !!result.available;
      renderSessionHistory(adaptiveData.recentSessions.slice(0, 1), {
        wrap: $('#aq-session-history-list'),
        empty: $('#aq-session-history-empty'),
        showReviewButton: false,
        allowFollowUp: false
      });
      renderAdaptiveDashboard();
    });

    requests.mistakeGroups.done(function (result) {
      if (isStaleDashboardLoad()) return;
      adaptiveData.mistakeGroups = result.groups || [];
      adaptiveData.mistakeJournalAvailable = !!result.available;
      renderMistakeJournal(adaptiveData.mistakeGroups);
      renderAdaptiveDashboard();
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
    resetConfidenceSessionState();
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
        if (data && data.success === false) {
          if (data.error === 'challenge_not_ready') {
            alert(data.message || 'Challenge mode is not available for this lecture yet.');
            showScreen('mode');
            refreshChallengeReadiness();
            return;
          }
          alert((data && data.message) || (data && data.error) || 'Could not start quiz.');
          showScreen('mode');
          return;
        }
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
    resetConfidenceSessionState();
    jQuery.ajax({
      type: 'POST', url: urlFinalizeSession,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (!data.success) {
          if (data.error === 'challenge_not_ready') {
            alert(data.message || 'Challenge mode is not available for this lecture yet.');
            showScreen('mode');
            refreshChallengeReadiness();
            return;
          }
          alert(data.message || data.error || 'Could not start quiz. Please try again.');
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

  var helpBtn = $('#aq-btn-help');
  if (helpBtn) helpBtn.onclick = function () { showScreen('help'); };

  ['#aq-btn-help-back', '#aq-btn-help-back-bottom'].forEach(function (sel) {
    var btn = $(sel);
    if (btn) btn.onclick = function () { showScreen('start'); };
  });

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

  ['low', 'medium', 'high'].forEach(function (level) {
    var chip = $('#aq-confidence-' + level);
    if (chip) {
      chip.onclick = function () { setConfidenceSelection(level); };
    }
  });

  var confidenceCloseBtn = $('#aq-confidence-close');
  if (confidenceCloseBtn) confidenceCloseBtn.onclick = function (event) {
    event.stopPropagation();
    toggleConfidenceDismissMenu();
  };

  var confidenceHideQuestionBtn = $('#aq-confidence-hide-question');
  if (confidenceHideQuestionBtn) confidenceHideQuestionBtn.onclick = function () { handleConfidenceDismiss('question'); };

  var confidenceHideSessionBtn = $('#aq-confidence-hide-session');
  if (confidenceHideSessionBtn) confidenceHideSessionBtn.onclick = function () { handleConfidenceDismiss('session'); };

  var confidenceCancelBtn = $('#aq-confidence-dismiss-cancel');
  if (confidenceCancelBtn) confidenceCancelBtn.onclick = function () { closeConfidenceDismissMenu(); };

  var recoveryStartBtn = $('#aq-btn-recovery-start');
  if (recoveryStartBtn) recoveryStartBtn.onclick = handleStartRecoveryStep;

  var recoverySkipBtn = $('#aq-btn-recovery-skip');
  if (recoverySkipBtn) recoverySkipBtn.onclick = handleDeclineRecoveryStep;

  var workedContinueBtn = $('#aq-btn-worked-example-continue');
  if (workedContinueBtn) workedContinueBtn.onclick = handleContinueFromWorkedExample;

  var workedPracticeBtn = $('#aq-btn-worked-example-practice');
  if (workedPracticeBtn) workedPracticeBtn.onclick = handlePracticeRecoveryStep;

  element.addEventListener('click', function (event) {
    var wrap = $('#aq-confidence-box');
    if (!wrap || wrap.classList.contains('aq-hidden')) return;
    if (!state.confidenceDismissMenuOpen) return;
    if (wrap.contains(event.target)) return;
    closeConfidenceDismissMenu();
  });

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
  if (courseBackBtn) courseBackBtn.onclick = function () {
    closeResumePrompt();
    showScreen('start');
  };

  var courseContinueBtn = $('#aq-btn-course-continue');
  if (courseContinueBtn) {
    courseContinueBtn.onclick = function () {
      var checked = element.querySelector('#aq-course-list input[type=radio]:checked');
      if (!checked) { alert('Please select a course.'); return; }
      selectedCourseId = checked.value;
      selectedCourseName = checked.getAttribute('data-course-name') || checked.value;
      if (pickerMode === 'progress') loadDashboard('start', selectedCourseId, selectedCourseName);
      else if (pickerMode === 'history') loadFullSessionHistory(selectedCourseId, 'start');
      else checkForResumableSession();
    };
  }

  var resumeContinueBtn = $('#aq-btn-resume-continue');
  if (resumeContinueBtn) {
    resumeContinueBtn.onclick = function () {
      var session = state.resumePromptSession;
      if (!session || state.resumeActionPending) return;

      state.resumeActionPending = true;
      resumeContinueBtn.disabled = true;
      resumeContinueBtn.textContent = 'Continuing…';
      var startNewBtn = $('#aq-btn-resume-start-new');
      if (startNewBtn) startNewBtn.disabled = true;

      jQuery.ajax({
        type: 'POST',
        url: urlResumeSession,
        data: JSON.stringify({
          selected_course_id: selectedCourseId,
          session_id: session.session_id
        }),
        contentType: 'application/json',
        success: function (data) {
          if (!data || !data.success || !data.question) {
            state.resumeActionPending = false;
            if (startNewBtn) startNewBtn.disabled = false;
            resumeContinueBtn.disabled = false;
            resumeContinueBtn.textContent = 'Continue Previous Quiz';
            alert((data && data.error) || 'Could not resume previous quiz.');
            closeResumePrompt();
            continueAfterCourseSelection();
            return;
          }

          selectedMode = data.selected_mode || selectedMode;
          selectedContentIds = Array.isArray(data.selected_content_ids) ? data.selected_content_ids.slice() : [];
          if (typeof data.questions_seen === 'number') state.questionsSeenSoFar = data.questions_seen;
          if (typeof data.session_score === 'number') state.sessionScore = data.session_score;
          if (data.max_questions) state.maxQuestionsCurrent = data.max_questions;
          closeResumePrompt();
          renderQuestion(data);
        },
        error: function () {
          state.resumeActionPending = false;
          if (startNewBtn) startNewBtn.disabled = false;
          resumeContinueBtn.disabled = false;
          resumeContinueBtn.textContent = 'Continue Previous Quiz';
          alert('Could not resume previous quiz.');
          closeResumePrompt();
          continueAfterCourseSelection();
        }
      });
    };
  }

  var resumeStartNewBtn = $('#aq-btn-resume-start-new');
  if (resumeStartNewBtn) {
    resumeStartNewBtn.onclick = function () {
      var session = state.resumePromptSession;
      if (!session || state.resumeActionPending) return;

      state.resumeActionPending = true;
      resumeStartNewBtn.disabled = true;
      resumeStartNewBtn.textContent = 'Starting New…';
      var continueBtn = $('#aq-btn-resume-continue');
      if (continueBtn) continueBtn.disabled = true;

      jQuery.ajax({
        type: 'POST',
        url: urlRetireResumableSession,
        data: JSON.stringify({
          selected_course_id: selectedCourseId,
          session_id: session.session_id
        }),
        contentType: 'application/json',
        success: function (data) {
          if (!data || !data.success) {
            state.resumeActionPending = false;
            resumeStartNewBtn.disabled = false;
            resumeStartNewBtn.textContent = 'Start a New Quiz';
            if (continueBtn) continueBtn.disabled = false;
            alert((data && data.error) || 'Could not start a new quiz right now.');
            return;
          }

          closeResumePrompt();
          continueAfterCourseSelection();
        },
        error: function () {
          state.resumeActionPending = false;
          resumeStartNewBtn.disabled = false;
          resumeStartNewBtn.textContent = 'Start a New Quiz';
          if (continueBtn) continueBtn.disabled = false;
          alert('Could not start a new quiz right now.');
        }
      });
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

      state.modeTouchedByUser = false;
      state.modeFraming = null;
      state.diagnosticRequired = {
        required: false,
        reason: '',
        affectedCount: 0,
        selectedCount: selectedContentIds.length
      };
      showScreen('mode');
      refreshChallengeReadiness();
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

      if (selectedMode === 'challenge' && !state.challengeReadiness.ready) {
        alert(state.challengeReadiness.message || 'Challenge mode is not available for this lecture yet.');
        refreshChallengeReadiness();
        return;
      }

      startSessionWithIds(selectedContentIds, selectedCourseId, selectedMode || 'normal_practice');
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
      if (!getReviewItems().length) return;
      if (reviewState.questionIndex > 0) {
        reviewState.questionIndex -= 1;
        renderReviewQuestion();
      }
    };
  }

  var reviewNextBtn = $('#aq-btn-review-next');
  if (reviewNextBtn) {
    reviewNextBtn.onclick = function () {
      var items = getReviewItems();
      if (!items.length) return;
      if (reviewState.questionIndex < items.length - 1) {
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
      var items = getReviewItems();
      if (items.length &&
        reviewState.questionIndex < items.length - 1) {
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
