"""
AdaptiveQuizXBlock
Calls the FastAPI backend for all adaptive logic.
XBlock fields store only rendering/session state (never adaptive state).
All mastery, IRT, and difficulty live in MongoDB via the FastAPI backend.
"""

import json
import logging
import pkg_resources
import requests

from xblock.core import XBlock
from xblock.fields import Scope, Integer, String, Boolean
from xblock.fragment import Fragment

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default FastAPI backend URL — can override in Studio
# ---------------------------------------------------------------------------
DEFAULT_BACKEND_URL = "http://host.docker.internal:8100"


class AdaptiveQuizXBlock(XBlock):
    """
    AI-Powered Adaptive Quiz XBlock.

    Instructor-configurable fields (set in Studio):
      - display_name
      - course_id       (logical course identifier sent to backend)
      - backend_url     (FastAPI server base URL)
      - max_questions   (session length)

    Per-student session fields (Scope.user_state):
      - questions_seen  (count this session)
      - session_score   (correct answers this session)
      - session_active  (whether a session is in progress)
      - current_topic   (topic of current question)
      - current_difficulty
      - current_question_json  (serialized question dict)
      - session_topics  (topics available this session, serialized JSON list)
      - session_source_text    (source text chosen for this session)
    """

    # ------------------------------------------------------------------ #
    # Instructor-facing fields (Scope.settings = editable in Studio)      #
    # ------------------------------------------------------------------ #
    display_name = String(
        display_name="Display Name",
        default="Adaptive Quiz",
        scope=Scope.settings,
        help="Name shown to students above the quiz block.",
    )

    course_id = String(
        display_name="Course ID",
        default="demo_course_01",
        scope=Scope.settings,
help="Default fallback course identifier used if no learner-selected course is active.",
    )

    backend_url = String(
        display_name="Backend URL",
        default=DEFAULT_BACKEND_URL,
        scope=Scope.settings,
        help="Base URL of the FastAPI adaptive quiz backend (no trailing slash).",
    )

    max_questions = Integer(
        display_name="Questions Per Session",
        default=10,
        scope=Scope.settings,
        help="How many questions a student answers before seeing their session score.",
    )

    # ------------------------------------------------------------------ #
    # Per-student session fields (Scope.user_state)                       #
    # ------------------------------------------------------------------ #
    questions_seen = Integer(default=0, scope=Scope.user_state)
    session_score = Integer(default=0, scope=Scope.user_state)
    session_active = Boolean(default=False, scope=Scope.user_state)
    current_topic = String(default="", scope=Scope.user_state)
    current_difficulty = Integer(default=3, scope=Scope.user_state)
    current_question_json = String(default="", scope=Scope.user_state)
    session_topics_json = String(default="", scope=Scope.user_state)
    session_source_text = String(default="", scope=Scope.user_state)
    session_target_questions = Integer(default=0, scope=Scope.user_state)
    selected_course_id = String(default="", scope=Scope.user_state)
    active_session_id = String(default="", scope=Scope.user_state)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _student_id(self):
        """Return a stable anonymous student ID from the Open edX runtime."""
        return self.runtime.anonymous_student_id

    def _api(self, path, method="POST", payload=None):
        """Make a synchronous call to the FastAPI backend."""
        url = f"{self.backend_url}{path}"
        try:
            if method == "GET":
                resp = requests.get(url, timeout=30)
            else:
                resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.error("Backend call failed [%s %s]: %s", method, url, exc)
            return None

    def resource_string(self, path):
        """Return the contents of a static resource file."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")
    
    def _active_course_id(self):
        """Use the learner-selected course if available, otherwise fall back to Studio default."""
        return self.selected_course_id or self.course_id

    # ------------------------------------------------------------------ #
    # Student view                                                         #
    # ------------------------------------------------------------------ #

    def student_view(self, context=None):
        """Render the student-facing quiz UI."""
        html = self.resource_string("static/html/quiz.html")
        frag = Fragment(html)
        frag.add_css(self.resource_string("static/css/quiz.css"))
        frag.add_javascript(self.resource_string("static/js/quiz.js"))

        # Pass configuration to JS
        frag.initialize_js("AdaptiveQuizXBlock", {
            "max_questions": self.max_questions,
            "display_name": self.display_name,
            "session_active": self.session_active,
            "questions_seen": self.questions_seen,
            "session_score": self.session_score,
        })
        return frag

    # ------------------------------------------------------------------ #
    # Studio (author) view                                                 #
    # ------------------------------------------------------------------ #

    def studio_view(self, context=None):
        save_url = self.runtime.handler_url(self, "studio_submit")

        html = f"""
        <div class="aq-studio-editor">
        <h2>Adaptive Quiz Settings</h2>
        <form id="aq-studio-form">

            <label>Display Name
            <input type="text" name="display_name" value="{self.display_name}" />
            </label>

            <label>Default Course ID
            <input type="text" name="course_id" value="{self.course_id}" />
            <small>Optional fallback course if no learner-selected course is active.</small>
            </label>

            <label>Backend URL
            <input type="text" name="backend_url" value="{self.backend_url}" />
            <small>Base URL of the FastAPI server (e.g. http://host.docker.internal:8100).</small>
            </label>

            <label>Questions Per Session
            <input type="number" name="max_questions" value="{self.max_questions}" min="1" max="50" />
            </label>

            <button type="submit" class="button action-primary">Save</button>
        </form>
        </div>

        <style>
        .aq-studio-editor {{ font-family: sans-serif; padding: 20px; }}
        .aq-studio-editor label {{ display: block; margin-bottom: 14px; font-weight: bold; }}
        .aq-studio-editor input {{ display: block; width: 100%; max-width: 480px;
            padding: 6px 8px; margin-top: 4px; border: 1px solid #ccc; border-radius: 4px; }}
        .aq-studio-editor small {{ display: block; color: #666; font-weight: normal; margin-top: 2px; }}
        .aq-studio-note {{ background: #fff8e1; border-left: 4px solid #ffc107;
            padding: 10px 14px; margin: 16px 0; border-radius: 4px; font-weight: normal; }}
        .aq-studio-editor button {{ margin-top: 12px; padding: 8px 20px; }}
        </style>

        <script>
        (function() {{
            var form = document.getElementById('aq-studio-form');
            if (!form) return;

            form.addEventListener('submit', function(e) {{
            e.preventDefault();

            var data = {{}};
            new FormData(form).forEach(function(v, k) {{
                data[k] = v;
            }});

            data.max_questions = parseInt(data.max_questions, 10);

            jQuery.ajax({{
                type: 'POST',
                url: '{save_url}',
                data: JSON.stringify(data),
                contentType: 'application/json',
                success: function() {{
                alert('Adaptive Quiz settings saved successfully.');
                window.location.reload();
                }},
                error: function(xhr) {{
                console.error('studio_submit failed', xhr);
                alert('Failed to save Adaptive Quiz settings.');
                }}
            }});
            }});
        }})();
        </script>
        """
        return Fragment(html)

    # ------------------------------------------------------------------ #
    # Handlers — called by quiz.js via runtime.handlerUrl()              #
    # ------------------------------------------------------------------ #

    @XBlock.json_handler
    def start_session(self, data, suffix=""):
        requested_q = int(data.get("question_count", self.max_questions))
        requested_q = max(1, min(50, requested_q))
        self.session_target_questions = requested_q

        student_id = self._student_id()
        selected_course = data.get("selected_course_id") or self._active_course_id()
        content_ids = data.get("content_ids", [])

        # Persist selected learner course for the rest of the session
        self.selected_course_id = selected_course

        self.questions_seen = 0
        self.session_score = 0
        self.session_active = True
        self.session_source_text = ""
        self.session_topics_json = json.dumps([])

        if not content_ids:
            return {"success": False, "error": "Please select at least one content item."}

        start_resp = self._api("/api/quiz/session/start", payload={
            "student_id": student_id,
            "course_id": selected_course,
            "topic": "",
            "source_text": "",
            "content_ids": content_ids,
            "question_count": requested_q,
        })

        if not start_resp:
            return {"success": False, "error": "Could not reach quiz backend."}
        
        self.active_session_id = start_resp.get("session_id", "")
        self.session_topics_json = json.dumps(start_resp.get("topics", []))
        self.session_source_text = start_resp.get("resolved_source_text", "")
        self.current_difficulty = start_resp.get("current_difficulty", 3)
        self.current_topic = start_resp.get("topics", [""])[0] if start_resp.get("topics") else ""

        return self._fetch_and_store_question()

    @XBlock.json_handler
    def get_question(self, data, suffix=""):
        """Fetch a new question (called after submitting an answer)."""
        return self._fetch_and_store_question()

    @XBlock.json_handler
    def submit_answer(self, data, suffix=""):
        """
        Student submits an answer.
        1. POST /api/quiz/submit to update mastery + get next params.
        2. Return correctness + explanation + updated mastery.
        3. Tell the frontend whether the session is complete.
        """
        selected = data.get("selected_answer", "")
        question = json.loads(self.current_question_json) if self.current_question_json else {}

        if not question:
            return {"success": False, "error": "No active question found."}

        student_id = self._student_id()
        time_spent_ms = int(data.get("time_spent_ms", 15000))

        submit_resp = self._api("/api/quiz/submit", payload={
            "student_id": student_id,
            "course_id": self._active_course_id(),
            "question_id": question.get("question", "")[:80],
            "selected_answer": selected,
            "correct_answer": question.get("correct_answer", ""),
            "topic": self.current_topic,
            "difficulty": self.current_difficulty,
            "time_spent_ms": time_spent_ms,
            "session_id": self.active_session_id or None,
        })

        if not submit_resp:
            return {"success": False, "error": "Could not reach quiz backend."}

        is_correct = submit_resp.get("is_correct", False)
        if is_correct:
            self.session_score += 1
        self.questions_seen += 1

        # Update local state from backend's adaptive decision
        self.current_difficulty = submit_resp.get("next_difficulty", self.current_difficulty)
        self.current_topic = submit_resp.get("next_topic", self.current_topic)

        target_questions = self.session_target_questions or self.max_questions
        backend_complete = submit_resp.get("session_complete", False)
        session_complete = backend_complete or (self.questions_seen >= target_questions)

        if session_complete:
            self.session_active = False
            self.active_session_id = ""
            grade_pct = self.session_score / target_questions
            self.runtime.publish(self, "grade", {
                "value": grade_pct,
                "max_value": 1.0,
            })

        return {
            "success": True,
            "is_correct": is_correct,
            "correct_answer": question.get("correct_answer", ""),
            "explanation": question.get("explanation", ""),
            "updated_mastery": submit_resp.get("updated_mastery", 0.5),
            "next_difficulty": self.current_difficulty,
            "support_features": submit_resp.get("support_features", []),
            "questions_seen": self.questions_seen,
            "session_score": self.session_score,
            "max_questions": target_questions,
            "session_complete": session_complete,
            "session_recommendation": submit_resp.get("session_recommendation"),
            "weakest_topic_this_session": submit_resp.get("weakest_topic_this_session"),
            "strongest_topic_this_session": submit_resp.get("strongest_topic_this_session"),
            "session_accuracy": submit_resp.get("session_accuracy"),
            "avg_time_spent_ms": submit_resp.get("avg_time_spent_ms"),
            "narrative_bridge": submit_resp.get("narrative_bridge"),
        }

    @XBlock.json_handler
    def explain_simpler(self, data, suffix=""):
        """Proxy to /api/quiz/support/explain for simpler re-explanation."""
        question = json.loads(self.current_question_json) if self.current_question_json else {}
        resp = self._api("/api/quiz/support/explain", payload={
            "topic": self.current_topic,
            "question": question.get("question", ""),
            "explanation": question.get("explanation", ""),
        })
        if resp:
            return {"success": True, "simpler_explanation": resp.get("simpler_explanation", "")}
        return {"success": False, "error": "Could not reach backend."}

    @XBlock.json_handler
    def similar_question(self, data, suffix=""):
        """Proxy to /api/quiz/support/similar for one-more-like-this."""
        resp = self._api("/api/quiz/support/similar", payload={
            "student_id": self._student_id(),
            "course_id": self._active_course_id(),
            "topic": self.current_topic,
            "difficulty": self.current_difficulty,
            "source_text": self.session_source_text,
        })
        if resp:
            self.current_question_json = json.dumps(resp)
            return {"success": True, "question": resp}
        return {"success": False, "error": "Could not generate similar question."}
    
    @XBlock.json_handler
    def get_progress(self, data, suffix=""):
        """Return dashboard data by combining mastery + state + course content."""
        student_id = self._student_id()
        active_course = data.get("selected_course_id") or self._active_course_id()

        if active_course:
            self.selected_course_id = active_course

        mastery_resp = self._api(
            f"/api/quiz/mastery/{student_id}/{active_course}",
            method="GET"
        )
        state_resp = self._api(
            f"/api/quiz/state/{student_id}/{active_course}",
            method="GET"
        )
        content_resp = self._api(
            f"/api/quiz/content/{active_course}",
            method="GET"
        )

        content_items = content_resp.get("items", []) if content_resp else []

        # No progress yet → return empty dashboard instead of error
        if not mastery_resp and not state_resp:
            return {
                "success": True,
                "has_progress": False,
                "student_id": student_id,
                "course_id": active_course,
                "topic_mastery": {},
                "topic_labels": {},
                "weak_topics": [],
                "strong_topics": [],
                "session_count": 0,
                "total_answers": 0,
                "overall_accuracy": None,
                "current_difficulty": 3,
                "content_items": content_items,
            }

        if not mastery_resp:
            return {"success": False, "error": "Mastery endpoint failed."}

        if not state_resp:
            return {"success": False, "error": "State endpoint failed."}

        return {
            "success": True,
            "has_progress": True,
            "student_id": student_id,
            "course_id": active_course,
            "topic_mastery": mastery_resp.get("topic_mastery", {}),
            "topic_labels": mastery_resp.get("topic_labels", {}),
            "weak_topics": mastery_resp.get("weak_topics", []),
            "strong_topics": mastery_resp.get("strong_topics", []),
            "session_count": state_resp.get("completed_sessions", state_resp.get("session_count", 0)),
            "total_answers": state_resp.get("completed_questions_answered", state_resp.get("total_answers", 0)),
            "overall_accuracy": state_resp.get("overall_accuracy"),
            "current_difficulty": state_resp.get("current_difficulty", 3),
            "content_items": content_items,
        }

    @XBlock.json_handler
    def studio_submit(self, data, suffix=""):
        """Save Studio editor fields."""
        self.display_name = data.get("display_name", self.display_name)
        self.course_id = data.get("course_id", self.course_id)
        self.backend_url = data.get("backend_url", self.backend_url)
        self.max_questions = int(data.get("max_questions", self.max_questions))
        return {"success": True}
    
    @XBlock.json_handler
    def get_content(self, data, suffix=""):
        selected_course = data.get("selected_course_id") or self._active_course_id()
        resp = self._api(f"/api/quiz/content/{selected_course}", method="GET")
        if resp:
            return {
                "success": True,
                "course_id": selected_course,
                "items": resp.get("items", [])
            }
        return {"success": True, "course_id": selected_course, "items": []}
    
    @XBlock.json_handler
    def get_courses(self, data, suffix=""):
        resp = self._api("/api/quiz/courses", method="GET")
        if resp:
            return {"success": True, "courses": resp.get("courses", [])}
        return {"success": True, "courses": []}
    
    @XBlock.json_handler
    def get_session_history(self, data, suffix=""):
        """Return recent completed session history for the selected course."""
        student_id = self._student_id()
        active_course = data.get("selected_course_id") or self._active_course_id()

        if active_course:
            self.selected_course_id = active_course

        resp = self._api(
            f"/api/quiz/sessions/{student_id}/{active_course}?limit=5",
            method="GET"
        )

        if resp:
            return {"success": True, "sessions": resp.get("sessions", [])}
        return {"success": True, "sessions": []}

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _fetch_and_store_question(self):
        """Call /api/quiz/generate, store result in user_state, return to JS."""
        topics = json.loads(self.session_topics_json) if self.session_topics_json else ["General"]

        # Rotate topic if multiple topics available
         # Backend's next_topic drives this after first answer; before that pick first
        topic = self.current_topic if self.current_topic else topics[0]
        if topic not in topics:
            topic = topics[0]
            self.current_topic = topic
        resp = self._api("/api/quiz/generate", payload={
            "student_id": self._student_id(),
            "course_id": self._active_course_id(),
            "topic": topic,
            "difficulty": self.current_difficulty,
            "source_text": self.session_source_text,
        })

        if not resp:
            return {"success": False, "error": "Could not generate question. Try again."}

        # Store question so submit_answer can reference it
        self.current_question_json = json.dumps(resp)
        self.current_topic = resp.get("topic", topic)
        self.current_difficulty = resp.get("difficulty", self.current_difficulty)

        return {
            "success": True,
            "question": resp,
            "questions_seen": self.questions_seen,
            "max_questions": self.session_target_questions or self.max_questions,
            "current_difficulty": self.current_difficulty,
        }

    @staticmethod
    def workbench_scenarios():
        """Scenarios for the XBlock Workbench (dev testing without full Open edX)."""
        return [
            ("Adaptive Quiz — Default", "<adaptivequiz/>"),
            ("Adaptive Quiz — 5 Questions", '<adaptivequiz max_questions="5"/>'),
        ]