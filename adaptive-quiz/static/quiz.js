const API = "http://localhost:8100";

// Session state
const session = {
  studentId: "student_" + Math.random().toString(36).slice(2, 8),
  courseId: "CS101",
  topic: "recursion",
  sourceText: "",
  mode: "auto",
  currentQuestion: null,
  questionCount: 0,
  currentDifficulty: 2,
  currentMastery: 0.5,
  answerStartTime: null,
};

// ── Session start ─────────────────────────────────────────────────────────────
async function startSession() {
  console.log("Student ID:", session.studentId);
  session.topic      = document.getElementById("topicInput").value.trim();
  session.sourceText = document.getElementById("sourceText").value.trim();
  session.mode       = document.getElementById("modeSelect").value;

  document.getElementById("configPanel").style.display    = "none";
  document.getElementById("masterySection").style.display = "block";
  document.getElementById("sessionInfo").style.display    = "flex";
  document.getElementById("qCounter").style.display       = "block";

  updateMasteryUI(session.topic, 0.5);
  updateBadges(2, session.mode, false);

  // Call session/start to init state + trigger background cache fill
  try {
    const resp = await fetch(`${API}/api/quiz/session/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        student_id:  session.studentId,
        course_id:   session.courseId,
        topic:       session.topic,
        difficulty:  session.currentDifficulty,
        source_text: session.sourceText,
        mode:        session.mode,
      }),
    });
    const info = await resp.json();
    // Restore mastery from existing state if student returning
    if (info.topic_mastery && info.topic_mastery[session.topic]) {
      session.currentMastery    = info.topic_mastery[session.topic];
      session.currentDifficulty = info.current_difficulty;
      updateMasteryUI(session.topic, session.currentMastery);
      updateBadges(session.currentDifficulty, session.mode, info.irt_active);
    }
  } catch (e) {
    console.warn("Session start failed, continuing anyway:", e);
  }

  loadNextQuestion();
}

// ── Load next question ────────────────────────────────────────────────────────
async function loadNextQuestion() {
  // Reset UI
  document.getElementById("nextBtn").style.display         = "none";
  document.getElementById("feedback").style.display        = "none";
  document.getElementById("supportFeatures").style.display = "none";
  document.getElementById("questionCard").style.display    = "none";
  document.getElementById("optionsContainer").innerHTML    = "";
  document.getElementById("btnExplainSimpler").style.display = "none";
  document.getElementById("btnOneMore").style.display        = "none";
  document.getElementById("loading").style.display           = "block";

  session.questionCount++;
  document.getElementById("qCounter").textContent = `Question ${session.questionCount}`;

  try {
    const resp = await fetch(`${API}/api/quiz/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        student_id:  session.studentId,
        course_id:   session.courseId,
        topic:       session.topic,
        difficulty:  session.currentDifficulty,
        source_text: session.sourceText,
        mode:        session.mode,
      }),
    });

    const q = await resp.json();
    session.currentQuestion = q;
    session.answerStartTime = Date.now();
    renderQuestion(q);

  } catch (e) {
    document.getElementById("loading").textContent =
      "❌ Failed to generate question. Is the server running?";
  }
}

// ── Render question ───────────────────────────────────────────────────────────
function renderQuestion(q) {
  document.getElementById("loading").style.display      = "none";
  document.getElementById("questionCard").style.display = "block";

  const diffLabel = { 1: "Easy", 2: "Medium", 3: "Hard" }[q.difficulty] || "Medium";
  document.getElementById("questionMeta").textContent =
    `Topic: ${q.topic}  ·  Difficulty: ${diffLabel}`;
  document.getElementById("questionText").textContent = q.question;

  const container = document.getElementById("optionsContainer");
  container.innerHTML = "";

  Object.entries(q.options).forEach(([key, text]) => {
    const btn = document.createElement("button");
    btn.className    = "option";
    btn.textContent  = `${key}.  ${text}`;
    btn.dataset.key  = key;
    btn.onclick      = () => selectAnswer(key, btn);
    container.appendChild(btn);
  });
}

// ── Select answer ─────────────────────────────────────────────────────────────
async function selectAnswer(selectedKey, clickedBtn) {
  const q      = session.currentQuestion;
  const timeMs = Date.now() - session.answerStartTime;
  const isCorrect = selectedKey === q.correct_answer;

  // Disable all options and color them
  document.querySelectorAll(".option").forEach(b => {
    b.disabled = true;
    if (b.dataset.key === q.correct_answer) b.classList.add("reveal");
  });
  clickedBtn.classList.add(isCorrect ? "correct" : "wrong");

  // Show feedback
  const fb = document.getElementById("feedback");
  fb.className   = `feedback ${isCorrect ? "correct" : "wrong"}`;
  fb.style.display = "block";
  document.getElementById("feedbackTitle").textContent =
    isCorrect ? "✅ Correct!" : "❌ Incorrect";
  document.getElementById("feedbackBody").textContent = q.explanation;

  // Submit to backend
  try {
    const resp = await fetch(`${API}/api/quiz/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        student_id:      session.studentId,
        course_id:       session.courseId,
        question_id:     `${q.topic}_${session.questionCount}`,
        selected_answer: selectedKey,
        correct_answer:  q.correct_answer,
        topic:           q.topic,
        difficulty:      q.difficulty,
        time_spent_ms:   timeMs,
      }),
    });

    const result = await resp.json();

    // Update session state
    session.currentDifficulty = result.next_difficulty;
    session.currentMastery    = result.updated_mastery;

    updateMasteryUI(q.topic, result.updated_mastery);
    updateBadges(result.next_difficulty, result.next_mode, session.questionCount >= 5);

    // Show support features if triggered
    if (result.support_features && result.support_features.length > 0) {
      document.getElementById("supportFeatures").style.display = "flex";
      if (result.support_features.includes("explain_simpler")) {
        document.getElementById("btnExplainSimpler").style.display = "inline-block";
      }
      if (result.support_features.includes("one_more_like_this")) {
        document.getElementById("btnOneMore").style.display = "inline-block";
      }
    }

  } catch (e) {
    console.error("Submit failed:", e);
  }

  document.getElementById("nextBtn").style.display = "block";
}

// ── Support features ──────────────────────────────────────────────────────────
async function explainSimpler() {
  document.getElementById("feedbackBody").textContent = "Generating simpler explanation...";
  document.getElementById("btnExplainSimpler").style.display = "none";

  try {
    const resp = await fetch(`${API}/api/quiz/support/explain`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question:    session.currentQuestion.question,
        explanation: session.currentQuestion.explanation,
        topic:       session.currentQuestion.topic,
      }),
    });
    const data = await resp.json();
    document.getElementById("feedbackBody").textContent =
      data.simpler_explanation || "Could not generate simpler explanation.";
  } catch (e) {
    document.getElementById("feedbackBody").textContent =
      session.currentQuestion.explanation +
      "\n\n(Tip: Try breaking the concept into smaller steps.)";
  }
}

function oneMoreLikeThis() {
  document.getElementById("btnOneMore").style.display = "none";
  document.getElementById("nextBtn").style.display    = "none";
  loadNextQuestion();
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function updateMasteryUI(topic, mastery) {
  const pct = Math.round(mastery * 100);
  document.getElementById("masteryTopic").textContent = `Mastery: ${topic}`;
  document.getElementById("masteryValue").textContent = `${pct}%`;
  document.getElementById("masteryBar").style.width   = `${pct}%`;
}

function updateBadges(difficulty, mode, irtActive) {
  const diffLabel = { 1: "Easy", 2: "Medium", 3: "Hard" }[difficulty] || "Medium";
  document.getElementById("difficultyBadge").textContent = `Difficulty: ${diffLabel}`;

  const modeEl = document.getElementById("modeBadge");
  modeEl.textContent = `Mode: ${mode.replace("_", " ")}`;
  modeEl.className   = "badge";
  if (mode === "weakness_review") modeEl.classList.add("mode-weakness");
  if (mode === "challenge")       modeEl.classList.add("mode-challenge");

  document.getElementById("irtBadge").textContent =
    irtActive ? "IRT: Active ✓" : "IRT: Warming up";
}