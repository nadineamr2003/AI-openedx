/* ── Adaptive Quiz XBlock — quiz.js ────────────────────────────────── */

function AdaptiveQuizXBlock(runtime, element, initArgs) {

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
    historySessions: [],
    historyPage: 0,
    historyPageSize: 3,
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
  var selectedMode = 'normal_practice';

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

    var overallAccuracy = '—';
    if (typeof data.overall_accuracy === 'number') {
      overallAccuracy = Math.round(data.overall_accuracy * 100) + '%';
    }

    var fields = {
      '#aq-dash-sessions': data.session_count || 0,
      '#aq-dash-total-answers': data.total_answers || 0,
      '#aq-dash-overall-accuracy': overallAccuracy,
      '#aq-dash-difficulty': DIFF_LABEL[data.current_difficulty || 3] || 'Medium'
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

      var actionsHtml = '';
      if (showReviewButton && session.question_log && session.question_log.length) {
        actionsHtml =
          '<div class="aq-session-actions">' +
          '<button class="aq-btn-session" type="button" data-session-index="' + idx + '">Review Session</button>' +
          '</div>';
      }

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
        '<span class="aq-session-mini-value">' + escapeHtml(session.strongest_topic_this_session || '—') + '</span>' +
        '</div>' +
        '<div class="aq-session-mini">' +
        '<span class="aq-session-mini-label">Needs Review</span>' +
        '<span class="aq-session-mini-value">' + escapeHtml(session.weakest_topic_this_session || '—') + '</span>' +
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
      wrap.querySelectorAll('.aq-btn-session').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var idx = parseInt(btn.getAttribute('data-session-index'), 10);
          openSessionReview(sessions[idx]);
        });
      });
    }
  }

  function updateHistoryPager() {
    var pagerWrap = $('#aq-history-pager-wrap');
    var pageInfo = $('#aq-history-page-info');
    var prevBtn = $('#aq-btn-history-prev');
    var nextBtn = $('#aq-btn-history-next');

    var total = state.historySessions.length;
    var pageSize = state.historyPageSize;
    var totalPages = total > 0 ? Math.ceil(total / pageSize) : 1;
    var currentPage = state.historyPage + 1;

    if (pagerWrap) {
      pagerWrap.classList.toggle('aq-hidden', total === 0);
    }

    if (pageInfo) {
      pageInfo.textContent = 'Page ' + currentPage + ' of ' + totalPages;
    }

    if (prevBtn) prevBtn.disabled = state.historyPage <= 0;
    if (nextBtn) nextBtn.disabled = state.historyPage >= totalPages - 1;
  }

  function renderHistoryPage() {
    var start = state.historyPage * state.historyPageSize;
    var end = start + state.historyPageSize;
    var pageSessions = state.historySessions.slice(start, end);

    renderSessionHistory(pageSessions, {
      wrap: $('#aq-history-list'),
      empty: $('#aq-history-empty'),
      showReviewButton: true
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
            showReviewButton: false
          });
        } else {
          renderSessionHistory([], {
            wrap: $('#aq-session-history-list'),
            empty: $('#aq-session-history-empty'),
            showReviewButton: false
          });
        }
      },
      error: function () {
        renderSessionHistory([], {
          wrap: $('#aq-session-history-list'),
          empty: $('#aq-session-history-empty'),
          showReviewButton: false
        });
      }
    });
  }

  function loadFullSessionHistory(courseId) {
    setLoading('Loading session history…');

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
  function startSessionWithIds(ids, courseId, mode) {
    var countInput = $('#aq-question-count');
    var chosenCount = countInput ? parseInt(countInput.value, 10) : MAX_Q;
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
        selected_course_id: courseId,
        content_ids: ids,
        mode: mode || 'normal_practice'
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

    if (titleEl) {
      titleEl.textContent = diagState.totalItems > 1
        ? 'Assessment · Lecture ' + (data.item_index + 1) + ' of ' + data.total_items
        : 'Quick Placement Assessment';
    }

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
    var diffBadge = $('#aq-diag-badge-diff');
    if (topicBadge) topicBadge.textContent = q.topic || data.topic || 'General';
    if (diffBadge) {
      var d = q.difficulty || data.difficulty || 3;
      diffBadge.textContent = DIFF_LABEL[d] || 'Medium';
      diffBadge.className = 'aq-tag aq-tag-diff ' + (DIFF_CLASS[d] || '');
    }

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
      loadFullSessionHistory(selectedCourseId);
    };
  }

  var historyBackBtn = $('#aq-btn-history-back');
  if (historyBackBtn) {
    historyBackBtn.onclick = function () {
      showScreen('dashboard');
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