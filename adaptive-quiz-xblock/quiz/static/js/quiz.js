/* ── Adaptive Quiz XBlock — quiz.js ────────────────────────────────── */
/* Called by Open edX as:  AdaptiveQuizXBlock(runtime, element, initArgs) */

function AdaptiveQuizXBlock(runtime, element, initArgs) {

  // ── Config ──────────────────────────────────────────────────────────
  var MAX_Q = initArgs.max_questions || 10;
  var DISPLAY_NAME = initArgs.display_name || "Adaptive Quiz";

  // ── Handler URLs (built by Open edX runtime) ────────────────────────
  var urlStart = runtime.handlerUrl(element, 'start_session');
  var urlSubmit = runtime.handlerUrl(element, 'submit_answer');
  var urlExplain = runtime.handlerUrl(element, 'explain_simpler');
  var urlSimilar = runtime.handlerUrl(element, 'similar_question');
  var urlProgress = runtime.handlerUrl(element, 'get_progress');

  // ── State ────────────────────────────────────────────────────────────
  var state = {
    currentQuestion: null,
    answered: false,
    questionStart: null,   // Date.now() when question was shown
    questionsSeenSoFar: initArgs.questions_seen || 0,
    sessionScore: initArgs.session_score || 0,

    // session summary values
    lastTopic: '—',
    lastMasteryPct: 50,
    lastDifficulty: 2,

    maxQuestionsCurrent: initArgs.max_questions || 10,
    dashboardOrigin: 'start',
  };

  // ── Shorthand DOM selectors scoped to this XBlock instance ──────────
  function $(sel) { return element.querySelector(sel); }

  // ── Show / hide screens ─────────────────────────────────────────────
  var SCREENS = ['start', 'loading', 'question', 'results', 'dashboard'];
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

  // ── Difficulty helpers ───────────────────────────────────────────────
  var DIFF_LABEL = { 1: 'Easy', 2: 'Medium', 3: 'Hard' };
  var DIFF_CLASS = { 1: 'diff-easy', 2: '', 3: 'diff-hard' };

  function updateHeader(question, questionsSeenNow) {
    var diffBadge = $('#aq-badge-diff');
    var topicBadge = $('#aq-badge-topic');
    var counter = $('#aq-counter');
    var progress = $('#aq-progress-bar');

    if (topicBadge) topicBadge.textContent = question.topic || 'General';
    if (counter)
      counter.textContent = 'Q ' + (questionsSeenNow + 1) + ' / ' + state.maxQuestionsCurrent;
    if (diffBadge) {
      var d = question.difficulty || 2;
      diffBadge.textContent = DIFF_LABEL[d] || 'Medium';
      diffBadge.className = 'aq-badge aq-badge-diff ' + (DIFF_CLASS[d] || '');
    }
    if (progress)
      progress.style.width = Math.round((questionsSeenNow / state.maxQuestionsCurrent) * 100) + '%';
  }

  // ── Render a question ────────────────────────────────────────────────
  function renderQuestion(resp) {
    if (!resp || !resp.success || !resp.question) {
      alert('Could not load question: ' + (resp && resp.error ? resp.error : 'Unknown error'));
      showScreen('start');
      return;
    }

    if (resp.max_questions) {
      state.maxQuestionsCurrent = resp.max_questions;
    }

    var q = resp.question;
    state.currentQuestion = q;
    state.answered = false;
    state.questionStart = Date.now();

    updateHeader(q, resp.questions_seen || state.questionsSeenSoFar);

    // Question text
    var qtEl = $('#aq-question-text');
    if (qtEl) qtEl.textContent = q.question;

    // Options
    ['A', 'B', 'C', 'D'].forEach(function (key) {
      var btn = $('#aq-opt-' + key);
      if (!btn) return;
      btn.textContent = key + '. ' + (q.options[key] || '');
      btn.className = 'aq-option';
      btn.disabled = false;
      btn.onclick = function () { handleOptionClick(key); };
    });

    // Hide feedback
    var fb = $('#aq-feedback');
    if (fb) fb.classList.add('aq-hidden');

    showScreen('question');
  }

  // ── Handle option click ──────────────────────────────────────────────
  function handleOptionClick(selectedKey) {
    if (state.answered) return;
    state.answered = true;

    // Mark selected
    var selBtn = $('#aq-opt-' + selectedKey);
    if (selBtn) selBtn.classList.add('selected');

    // Disable all options
    ['A', 'B', 'C', 'D'].forEach(function (k) {
      var b = $('#aq-opt-' + k);
      if (b) b.disabled = true;
    });

    var timeSpentMs = Date.now() - (state.questionStart || Date.now());

    // POST to submit_answer handler
    jQuery.ajax({
      type: 'POST',
      url: urlSubmit,
      data: JSON.stringify({
        selected_answer: selectedKey,
        time_spent_ms: timeSpentMs,
      }),
      contentType: 'application/json',
      success: function (data) { renderFeedback(data, selectedKey); },
      error: function () { alert('Network error submitting answer.'); },
    });
  }

  // ── Render feedback after answer ─────────────────────────────────────
  function renderFeedback(data, selectedKey) {
    if (!data || !data.success) {
      alert('Error: ' + (data && data.error ? data.error : 'Unknown'));
      return;
    }

    state.questionsSeenSoFar = data.questions_seen;
    state.sessionScore = data.session_score;

    if (data.max_questions) {
      state.maxQuestionsCurrent = data.max_questions;
    }

    //  keep latest values for results summary
    state.lastTopic = (state.currentQuestion && state.currentQuestion.topic) || 'General';
    state.lastMasteryPct = Math.round((data.updated_mastery || 0.5) * 100);
    state.lastDifficulty = data.next_difficulty || 2;

    // Colour options
    var correct = data.correct_answer;
    ['A', 'B', 'C', 'D'].forEach(function (k) {
      var b = $('#aq-opt-' + k);
      if (!b) return;
      if (k === correct) b.classList.add('correct');
      if (k === selectedKey && k !== correct) b.classList.add('incorrect');
    });

    // Feedback header
    var header = $('#aq-feedback-header');
    if (header) {
      header.textContent = data.is_correct ? '✅ Correct!' : '❌ Incorrect';
      header.className = 'aq-feedback-header ' + (data.is_correct ? 'correct' : 'incorrect');
    }

    // Explanation
    var expEl = $('#aq-explanation');
    if (expEl) expEl.textContent = data.explanation || '';

    // Mastery bar
    var pct = Math.round((data.updated_mastery || 0.5) * 100);
    var fillEl = $('#aq-mastery-fill');
    var pctEl = $('#aq-mastery-pct');
    if (fillEl) fillEl.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';

    // Support buttons
    var supportRow = $('#aq-support-row');
    if (supportRow) {
      supportRow.innerHTML = '';
      var features = data.support_features || [];
      if (features.indexOf('explain_simpler') !== -1) {
        supportRow.appendChild(makeSupportBtn('💬 Simpler explanation', handleExplainSimpler));
      }
      if (features.indexOf('one_more_like_this') !== -1) {
        supportRow.appendChild(makeSupportBtn('🔄 One more like this', handleSimilarQuestion));
      }
    }

    // Next button
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

  // ── Support handlers ─────────────────────────────────────────────────
  function handleExplainSimpler() {
    jQuery.ajax({
      type: 'POST', url: urlExplain,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (data.success) {
          var expEl = $('#aq-explanation');
          if (expEl) expEl.textContent = data.simpler_explanation;
        }
      },
    });
  }

  function handleSimilarQuestion() {
    setLoading('Generating a similar question…');
    jQuery.ajax({
      type: 'POST', url: urlSimilar,
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) {
        if (data.success) renderQuestion({
          success: true, question: data.question,
          questions_seen: state.questionsSeenSoFar
        });
        else showScreen('question');
      },
      error: function () { showScreen('question'); },
    });
  }

  // ── Load next question ───────────────────────────────────────────────
  function loadNextQuestion() {
    setLoading('Generating your next question…');
    jQuery.ajax({
      type: 'POST', url: runtime.handlerUrl(element, 'get_question'),
      data: JSON.stringify({}), contentType: 'application/json',
      success: function (data) { renderQuestion(data); },
      error: function () { alert('Could not load next question.'); showScreen('start'); },
    });
  }

  // ── Results screen ───────────────────────────────────────────────────
  function showResults(data) {
    var score = data.session_score || state.sessionScore;
    var pct = Math.round((score / state.maxQuestionsCurrent) * 100);
    var incorrect = state.maxQuestionsCurrent - score;

    var emojiEl = $('#aq-result-emoji');
    var numEl = $('#aq-score-num');
    var denomEl = $('#aq-score-denom');
    var msgEl = $('#aq-score-msg');

    if (emojiEl) emojiEl.textContent = pct >= 80 ? '🏆' : pct >= 60 ? '🎉' : '📚';
    if (numEl) numEl.textContent = score;
    if (denomEl) denomEl.textContent = '/ ' + state.maxQuestionsCurrent;
    if (msgEl) msgEl.textContent = pct >= 80
      ? 'Excellent! You\'ve mastered this material.'
      : pct >= 60
        ? 'Good work! Keep practicing to improve.'
        : 'Keep going — every attempt builds mastery!';

    // NEW: fill session summary
    var accuracyEl = $('#aq-summary-accuracy');
    var incorrectEl = $('#aq-summary-incorrect');
    var topicEl = $('#aq-summary-topic');
    var masteryEl = $('#aq-summary-mastery');
    var difficultyEl = $('#aq-summary-difficulty');

    if (accuracyEl) accuracyEl.textContent = pct + '%';
    if (incorrectEl) incorrectEl.textContent = incorrect;
    if (topicEl) topicEl.textContent = state.lastTopic || 'General';
    if (masteryEl) masteryEl.textContent = state.lastMasteryPct + '%';
    if (difficultyEl) difficultyEl.textContent = DIFF_LABEL[state.lastDifficulty] || 'Medium';

    // Update progress bar to 100%
    var pb = $('#aq-progress-bar');
    if (pb) pb.style.width = '100%';

    showScreen('results');
  }

  // ── Start session ────────────────────────────────────────────────────
  function startSession() {
    var countSelect = $('#aq-question-count');
    var chosenCount = countSelect ? parseInt(countSelect.value, 10) : MAX_Q;

    state.questionsSeenSoFar = 0;
    state.sessionScore = 0;
    state.lastTopic = '—';
    state.lastMasteryPct = 50;
    state.lastDifficulty = 2;

    setLoading('Preparing your adaptive quiz…');
    jQuery.ajax({
      type: 'POST',
      url: urlStart,
      data: JSON.stringify({ question_count: chosenCount }),
      contentType: 'application/json',
      success: function (data) {
        if (data && data.max_questions) {
          state.maxQuestionsCurrent = data.max_questions;
        } else {
          state.maxQuestionsCurrent = chosenCount;
        }
        renderQuestion(data);
      },
      error: function () {
        alert('Could not connect to the quiz backend. Please try again later.');
        showScreen('start');
      },
    });
  }

  // dashboard
  function renderDashboard(data) {
    if (!data || !data.success) {
      alert('Could not load progress dashboard.');
      showScreen('results');
      return;
    }

    var sessionsEl = $('#aq-dash-sessions');
    var totalAnswersEl = $('#aq-dash-total-answers');
    var irtEl = $('#aq-dash-irt');
    var diffEl = $('#aq-dash-difficulty');
    var topicsWrap = $('#aq-dashboard-topics');

    var emptyEl = $('#aq-dashboard-empty');

    var backBtn = $('#aq-btn-back-results');
    if (backBtn) {
      backBtn.textContent = state.dashboardOrigin === 'results' ? '← Back to Results' : '← Back to Home';
    }

    if (topicsWrap) {
      topicsWrap.innerHTML = '';
    }

    if (!data.has_progress || !data.topic_mastery || Object.keys(data.topic_mastery).length === 0) {
      if (emptyEl) emptyEl.classList.remove('aq-hidden');
      showScreen('dashboard');
      return;
    }

    if (emptyEl) emptyEl.classList.add('aq-hidden');

    if (sessionsEl) sessionsEl.textContent = data.session_count || 0;
    if (totalAnswersEl) totalAnswersEl.textContent = data.total_answers || 0;
    if (irtEl) irtEl.textContent = data.irt_active ? 'Active' : 'Warming up';
    if (diffEl) diffEl.textContent = DIFF_LABEL[data.current_difficulty || 2] || 'Medium';

    if (topicsWrap) {
      topicsWrap.innerHTML = '';

      var mastery = data.topic_mastery || {};
      var weakTopics = data.weak_topics || [];
      var strongTopics = data.strong_topics || [];

      Object.keys(mastery).forEach(function (topic) {
        var pct = Math.round((mastery[topic] || 0) * 100);

        var metaClass = 'normal';
        var metaText = pct + '%';
        if (weakTopics.indexOf(topic) !== -1) {
          metaClass = 'weak';
          metaText = pct + '% • Needs review';
        } else if (strongTopics.indexOf(topic) !== -1) {
          metaClass = 'strong';
          metaText = pct + '% • Strong';
        }

        var block = document.createElement('div');
        block.className = 'aq-dashboard-topic';
        block.innerHTML = `
        <div class="aq-dashboard-topic-row">
          <span class="aq-dashboard-topic-name">${topic}</span>
          <span class="aq-dashboard-topic-meta ${metaClass}">${metaText}</span>
        </div>
        <div class="aq-dashboard-bar-bg">
          <div class="aq-dashboard-bar-fill" style="width:${pct}%"></div>
        </div>
      `;
        topicsWrap.appendChild(block);
      });
    }

    showScreen('dashboard');
  }

  function loadDashboard(origin) {
    state.dashboardOrigin = origin || 'start';

    setLoading('Loading your progress…');
    jQuery.ajax({
      type: 'POST',
      url: urlProgress,
      data: JSON.stringify({}),
      contentType: 'application/json',
      success: function (data) {
        if (!data || !data.success) {
          alert('Dashboard error: ' + ((data && data.error) ? data.error : 'Unknown error'));
          showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
          return;
        }
        renderDashboard(data);
      },
      error: function (xhr) {
        alert('Could not load progress dashboard. HTTP ' + xhr.status);
        showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
      },
    });
  }

  // ── Wire up static buttons ───────────────────────────────────────────
  var startBtn = $('#aq-btn-start');
  if (startBtn) startBtn.onclick = startSession;

  var retryBtn = $('#aq-btn-retry');
  if (retryBtn) retryBtn.onclick = startSession;

  var progressBtn = $('#aq-btn-progress');
  if (progressBtn) {
    progressBtn.onclick = function () { loadDashboard('results'); };
  }

  var backResultsBtn = $('#aq-btn-back-results');
  if (backResultsBtn) {
    backResultsBtn.onclick = function () {
      showScreen(state.dashboardOrigin === 'results' ? 'results' : 'start');
    };
  }

  var dashRetryBtn = $('#aq-btn-dashboard-retry');
  if (dashRetryBtn) dashRetryBtn.onclick = startSession;

  var progressStartBtn = $('#aq-btn-progress-start');
  if (progressStartBtn) {
    progressStartBtn.onclick = function () { loadDashboard('start'); };
  }

  // ── Restore in-progress session ──────────────────────────────────────
  // If student refreshes mid-session, they see the start screen again.
  // (XBlock fields store count/score, backend has adaptive state — no data lost)
  showScreen('start');

  // Patch display_name into the start screen
  var titleEl = element.querySelector('.aq-title');
  if (titleEl) titleEl.textContent = DISPLAY_NAME;
}