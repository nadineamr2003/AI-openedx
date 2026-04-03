"""
AdaptiveQuizXBlock
Calls the FastAPI backend for all adaptive logic.
XBlock fields store only rendering/session state (never adaptive state).
All mastery, IRT, and difficulty live in MongoDB via the FastAPI backend.
"""

import random
import json
import logging
import pkg_resources
import requests

from xblock.core import XBlock
from xblock.fields import Scope, Integer, String, Float, List, Boolean
from xblock.fragment import Fragment

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Placeholder content pool — randomly selected per session start.
# Each entry has a topic list and a source_text the LLM will use.
# ---------------------------------------------------------------------------
PLACEHOLDER_CONTENT = [
    {
        "topics": ["Photosynthesis", "Cellular Respiration", "Chloroplasts"],
        "source_text": (
            "Photosynthesis is the process by which green plants, algae, and some bacteria "
            "convert light energy into chemical energy stored as glucose. It occurs in the "
            "chloroplasts, specifically using chlorophyll to absorb sunlight. The overall "
            "equation is: 6CO2 + 6H2O + light energy → C6H12O6 + 6O2. "
            "Photosynthesis has two main stages: the light-dependent reactions, which occur "
            "in the thylakoid membranes and produce ATP and NADPH, and the Calvin cycle "
            "(light-independent reactions), which occur in the stroma and use that energy "
            "to fix CO2 into glucose. Cellular respiration is essentially the reverse — "
            "glucose is broken down to release energy in the form of ATP. It occurs in the "
            "mitochondria and involves glycolysis, the Krebs cycle, and the electron "
            "transport chain. Aerobic respiration produces approximately 36–38 ATP per "
            "glucose molecule, making it far more efficient than anaerobic respiration."
        ),
    },
    {
        "topics": ["Newton's Laws", "Forces", "Momentum"],
        "source_text": (
            "Newton's three laws of motion form the foundation of classical mechanics. "
            "The first law (Law of Inertia) states that an object at rest stays at rest "
            "and an object in motion stays in motion unless acted upon by an external force. "
            "The second law defines force as F = ma: the net force on an object equals its "
            "mass multiplied by its acceleration. This means heavier objects require more "
            "force to accelerate at the same rate. The third law states that for every "
            "action there is an equal and opposite reaction — forces always come in pairs. "
            "Momentum is defined as p = mv (mass × velocity) and is conserved in closed "
            "systems. An impulse (force × time) changes an object's momentum. These laws "
            "break down at relativistic speeds (near the speed of light), where Einstein's "
            "special relativity must be used instead."
        ),
    },
    {
        "topics": ["HTTP Protocol", "REST APIs", "Status Codes"],
        "source_text": (
            "HTTP (HyperText Transfer Protocol) is the foundation of data communication "
            "on the web. It is a stateless, request-response protocol. A client sends an "
            "HTTP request to a server, which responds with a status code and optional body. "
            "Common methods include GET (retrieve data), POST (send data), PUT (replace "
            "resource), PATCH (partial update), and DELETE (remove resource). "
            "Status codes are grouped: 2xx means success (200 OK, 201 Created), 3xx means "
            "redirection, 4xx means client error (400 Bad Request, 401 Unauthorized, "
            "403 Forbidden, 404 Not Found), and 5xx means server error (500 Internal "
            "Server Error, 503 Service Unavailable). REST (Representational State Transfer) "
            "is an architectural style for designing APIs using HTTP. RESTful APIs are "
            "stateless, use standard HTTP methods, and treat everything as a resource "
            "identified by a URL. JSON is the most common format for REST API bodies."
        ),
    },
    {
        "topics": ["Supply and Demand", "Market Equilibrium", "Elasticity"],
        "source_text": (
            "Supply and demand are the core forces that determine prices in a free market. "
            "The law of demand states that as price increases, quantity demanded decreases "
            "(inverse relationship). The law of supply states that as price increases, "
            "quantity supplied increases (direct relationship). Market equilibrium is the "
            "point where supply equals demand — the equilibrium price and quantity. "
            "If price is above equilibrium, there is a surplus; below equilibrium, a shortage. "
            "Price elasticity of demand measures how sensitive consumers are to price changes: "
            "elastic demand (elasticity > 1) means consumers are very responsive, inelastic "
            "demand (elasticity < 1) means they are not. Necessities tend to be inelastic "
            "(e.g., insulin), while luxuries tend to be elastic. Shifts in supply or demand "
            "curves — caused by changes in income, preferences, input costs, or technology — "
            "move the equilibrium price and quantity."
        ),
    },
]

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
        help="Unique identifier for this course, used to namespace student state in MongoDB.",
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
    current_difficulty = Integer(default=2, scope=Scope.user_state)
    current_question_json = String(default="", scope=Scope.user_state)
    session_topics_json = String(default="", scope=Scope.user_state)
    session_source_text = String(default="", scope=Scope.user_state)
    session_target_questions = Integer(default=0, scope=Scope.user_state)

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

    def _pick_content(self):
        """Randomly pick a content bundle from the placeholder pool."""
        return random.choice(PLACEHOLDER_CONTENT)

    def resource_string(self, path):
        """Return the contents of a static resource file."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

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
        """Render the Studio editor — instructors set display_name, course_id, etc."""
        html = f"""
        <div class="aq-studio-editor">
          <h2>Adaptive Quiz Settings</h2>
          <form id="aq-studio-form">

            <label>Display Name
              <input type="text" name="display_name" value="{self.display_name}" />
            </label>

            <label>Course ID
              <input type="text" name="course_id" value="{self.course_id}" />
              <small>Namespace for student state in MongoDB. Use a stable identifier.</small>
            </label>

            <label>Backend URL
              <input type="text" name="backend_url" value="{self.backend_url}" />
              <small>Base URL of the FastAPI server (e.g. http://host.docker.internal:8100).</small>
            </label>

            <label>Questions Per Session
              <input type="number" name="max_questions" value="{self.max_questions}" min="1" max="50" />
            </label>

            <p class="aq-studio-note">
              <strong>Content:</strong> This prototype uses randomly selected built-in
              topic content. Future versions will support instructor-uploaded PDFs.
            </p>

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
          document.getElementById('aq-studio-form').addEventListener('submit', function(e) {{
            e.preventDefault();
            var data = {{}};
            new FormData(this).forEach(function(v, k) {{ data[k] = v; }});
            data.max_questions = parseInt(data.max_questions);
            Xblock.runtime.handler_url && runtime.notify('save', {{state: 'start'}});
            $.post(runtime.handlerUrl(element, 'studio_submit'), JSON.stringify(data))
              .done(function() {{ runtime.notify('save', {{state: 'end'}}); }});
          }});
        </script>
        """
        return Fragment(html)

    # ------------------------------------------------------------------ #
    # Handlers — called by quiz.js via runtime.handlerUrl()              #
    # ------------------------------------------------------------------ #

    @XBlock.json_handler
    def start_session(self, data, suffix=""):
        """
        Student clicks "Start Quiz".
        1. Pick a random content bundle.
        2. Call /api/quiz/session/start on the backend.
        3. Fetch the first question.
        """
        requested_q = int(data.get("question_count", self.max_questions))
        requested_q = max(1, min(50, requested_q))
        self.session_target_questions = requested_q
        
        content = self._pick_content()
        topics = content["topics"]
        source_text = content["source_text"]

        # Persist chosen content for this session (needed for /generate calls)
        self.session_source_text = source_text
        self.session_topics_json = json.dumps(topics)
        self.questions_seen = 0
        self.session_score = 0
        self.session_active = True

        student_id = self._student_id()

        # Tell backend to init state + warm cache
        start_resp = self._api("/api/quiz/session/start", payload={
            "student_id": student_id,
            "course_id": self.course_id,
            "topic": ", ".join(topics),
            "source_text": source_text,
        })

        if not start_resp:
            return {"success": False, "error": "Could not reach quiz backend."}

        # Remember backend-chosen difficulty and first topic
        self.current_difficulty = start_resp.get("current_difficulty", 2)
        self.current_topic = topics[0]

        # Fetch first question
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
            "course_id": self.course_id,
            "question_id": question.get("question", "")[:80],  # use truncated question as ID
            "selected_answer": selected,
            "correct_answer": question.get("correct_answer", ""),
            "topic": self.current_topic,
            "difficulty": self.current_difficulty,
            "time_spent_ms": time_spent_ms,
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
        session_complete = self.questions_seen >= target_questions

        if session_complete:
            self.session_active = False
            # Publish grade to Open edX gradebook
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
            "course_id": self.course_id,
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
        """Return all-time dashboard data by combining mastery + full state."""
        student_id = self._student_id()

        mastery_resp = self._api(
            f"/api/quiz/mastery/{student_id}/{self.course_id}",
            method="GET"
        )
        state_resp = self._api(
            f"/api/quiz/state/{student_id}/{self.course_id}",
            method="GET"
        )

        # No progress yet → return empty dashboard instead of error
        if not mastery_resp and not state_resp:
            return {
                "success": True,
                "has_progress": False,
                "student_id": student_id,
                "course_id": self.course_id,
                "topic_mastery": {},
                "weak_topics": [],
                "strong_topics": [],
                "session_count": 0,
                "total_answers": 0,
                "irt_active": False,
                "current_difficulty": 2,
            }

        if not mastery_resp:
            return {"success": False, "error": "Mastery endpoint failed."}

        if not state_resp:
            return {"success": False, "error": "State endpoint failed."}

        return {
            "success": True,
            "has_progress": True,
            "student_id": student_id,
            "course_id": self.course_id,
            "topic_mastery": mastery_resp.get("topic_mastery", {}),
            "weak_topics": mastery_resp.get("weak_topics", []),
            "strong_topics": mastery_resp.get("strong_topics", []),
            "session_count": state_resp.get("session_count", 0),
            "total_answers": state_resp.get("total_answers", 0),
            "irt_active": state_resp.get("irt_active", False),
            "current_difficulty": state_resp.get("current_difficulty", 2),
        }

    @XBlock.json_handler
    def studio_submit(self, data, suffix=""):
        """Save Studio editor fields."""
        self.display_name = data.get("display_name", self.display_name)
        self.course_id = data.get("course_id", self.course_id)
        self.backend_url = data.get("backend_url", self.backend_url)
        self.max_questions = int(data.get("max_questions", self.max_questions))
        return {"success": True}

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _fetch_and_store_question(self):
        """Call /api/quiz/generate, store result in user_state, return to JS."""
        topics = json.loads(self.session_topics_json) if self.session_topics_json else ["General"]

        # Rotate topic if multiple topics available
        # Backend's next_topic drives this after first answer; before that pick first
        topic = self.current_topic if self.current_topic else topics[0]

        resp = self._api("/api/quiz/generate", payload={
            "student_id": self._student_id(),
            "course_id": self.course_id,
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