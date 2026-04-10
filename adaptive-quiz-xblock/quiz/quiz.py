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

    def _api(self, path, method="POST", payload=None, timeout=30):
        """Make a synchronous call to the FastAPI backend."""
        url = f"{self.backend_url}{path}"
        try:
            if method == "GET":
                resp = requests.get(url, timeout=timeout)
            else:
                resp = requests.post(url, json=payload, timeout=timeout)
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
        parse_url = self.runtime.handler_url(self, "parse_pdf")
        save_content_url = self.runtime.handler_url(self, "save_content_item")
        list_content_url = self.runtime.handler_url(self, "list_content_studio")

        html = f"""
    <div class="aqs-root">
    <style>
    .aqs-root {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 760px;
    color: #111827;
    font-size: 14px;
    line-height: 1.5;
    }}

    .aqs-tabs {{
    display: flex;
    border-bottom: 2px solid #E5E7EB;
    margin-bottom: 24px;
    gap: 2px;
    }}

    .aqs-tab {{
    padding: 10px 20px;
    border: none;
    background: none;
    font-size: .875rem;
    font-weight: 700;
    color: #6B7280;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    transition: color .15s, border-color .15s;
    }}

    .aqs-tab:hover {{
    color: #374151;
    }}

    .aqs-tab.active {{
    color: #2B4EDE;
    border-bottom-color: #2B4EDE;
    }}

    .aqs-field {{
    margin-bottom: 18px;
    }}

    .aqs-label {{
    display: block;
    font-size: .75rem;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: #6B7280;
    margin-bottom: 6px;
    }}

    .aqs-label-hint {{
    font-weight: 400;
    text-transform: none;
    letter-spacing: 0;
    color: #9CA3AF;
    font-size: .72rem;
    }}

    .aqs-input,
    .aqs-textarea,
    .aqs-select {{
    display: block;
    width: 100%;
    box-sizing: border-box;
    padding: 10px 12px;
    border: 1.5px solid #E5E7EB;
    border-radius: 10px;
    font-size: .9rem;
    font-family: inherit;
    color: #111827;
    background: #fff;
    outline: none;
    transition: border-color .15s, box-shadow .15s;
    }}

    .aqs-input:focus,
    .aqs-textarea:focus,
    .aqs-select:focus {{
    border-color: #2B4EDE;
    box-shadow: 0 0 0 3px rgba(43,78,222,.10);
    }}

    .aqs-textarea {{
    resize: vertical;
    line-height: 1.6;
    }}

    .aqs-textarea[readonly] {{
    background: #F9FAFB;
    color: #6B7280;
    }}

    .aqs-hint {{
    display: block;
    margin-top: 5px;
    font-size: .78rem;
    color: #9CA3AF;
    }}

    .aqs-grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    }}

    .aqs-col-full {{
    grid-column: 1 / -1;
    }}

    .aqs-btn-primary {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 22px;
    background: #2B4EDE;
    color: #fff;
    border: none;
    border-radius: 10px;
    font-size: .9rem;
    font-weight: 700;
    font-family: inherit;
    cursor: pointer;
    transition: background .15s, transform .1s, box-shadow .15s;
    box-shadow: 0 2px 10px rgba(43,78,222,.22);
    }}

    .aqs-btn-primary:hover {{
    background: #1A3AB8;
    transform: translateY(-1px);
    box-shadow: 0 4px 14px rgba(43,78,222,.30);
    }}

    .aqs-btn-primary:disabled {{
    background: #9CA3AF;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
    }}

    .aqs-toggle {{
    display: inline-flex;
    background: #F3F4F6;
    border-radius: 999px;
    padding: 3px;
    gap: 2px;
    margin-bottom: 16px;
    }}

    .aqs-toggle-btn {{
    padding: 7px 18px;
    border: none;
    border-radius: 999px;
    background: transparent;
    font-size: .83rem;
    font-weight: 700;
    color: #6B7280;
    cursor: pointer;
    transition: background .15s, color .15s, box-shadow .15s;
    }}

    .aqs-toggle-btn.active {{
    background: #fff;
    color: #2B4EDE;
    box-shadow: 0 1px 4px rgba(0,0,0,.12);
    }}

    .aqs-drop-zone {{
    border: 2px dashed #CBD5E1;
    border-radius: 14px;
    padding: 32px 20px;
    text-align: center;
    cursor: pointer;
    background: #F8FAFC;
    transition: border-color .15s, background .15s;
    margin-bottom: 14px;
    user-select: none;
    }}

    .aqs-drop-zone:hover,
    .aqs-drop-zone.drag-over {{
    border-color: #2B4EDE;
    background: #EEF2FF;
    }}

    .aqs-drop-zone.has-file {{
    border-color: #059669;
    background: #ECFDF5;
    border-style: solid;
    }}

    .aqs-drop-icon {{
    font-size: 2rem;
    margin-bottom: 8px;
    }}

    .aqs-drop-title {{
    font-size: .92rem;
    font-weight: 700;
    color: #374151;
    margin-bottom: 3px;
    }}

    .aqs-drop-sub {{
    font-size: .78rem;
    color: #9CA3AF;
    }}

    .aqs-topics-editor {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    padding: 9px 10px;
    border: 1.5px solid #E5E7EB;
    border-radius: 10px;
    background: #fff;
    min-height: 46px;
    cursor: text;
    transition: border-color .15s, box-shadow .15s;
    }}

    .aqs-topics-editor:focus-within {{
    border-color: #2B4EDE;
    box-shadow: 0 0 0 3px rgba(43,78,222,.10);
    }}

    .aqs-topic-tag {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: #EEF2FF;
    color: #2B4EDE;
    border: 1px solid rgba(43,78,222,.18);
    border-radius: 999px;
    padding: 3px 10px 3px 12px;
    font-size: .78rem;
    font-weight: 700;
    white-space: nowrap;
    }}

    .aqs-topic-remove {{
    background: none;
    border: none;
    cursor: pointer;
    color: #93C5FD;
    font-size: .9rem;
    line-height: 1;
    padding: 0;
    }}

    .aqs-topic-remove:hover {{
    color: #DC2626;
    }}

    .aqs-topic-input {{
    border: none;
    outline: none;
    font-size: .85rem;
    font-family: inherit;
    color: #374151;
    flex: 1;
    min-width: 150px;
    padding: 2px 4px;
    background: transparent;
    }}

    .aqs-divider {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 28px 0 20px;
    color: #9CA3AF;
    font-size: .72rem;
    font-weight: 700;
    letter-spacing: .06em;
    text-transform: uppercase;
    }}

    .aqs-divider::before,
    .aqs-divider::after {{
    content: "";
    flex: 1;
    height: 1px;
    background: #E5E7EB;
    }}

    .aqs-content-item {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
    margin-bottom: 8px;
    background: #fff;
    }}

    .aqs-content-item-title {{
    font-size: .9rem;
    font-weight: 700;
    color: #111827;
    }}

    .aqs-content-item-meta {{
    font-size: .76rem;
    color: #6B7280;
    margin-top: 2px;
    }}

    .aqs-type-badge {{
    display: inline-flex;
    align-items: center;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: .7rem;
    font-weight: 700;
    white-space: nowrap;
    }}

    .aqs-type-lecture {{
    background: #EEF2FF;
    color: #2B4EDE;
    }}

    .aqs-status-success {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-radius: 10px;
    background: #ECFDF5;
    color: #065F46;
    font-size: .875rem;
    font-weight: 600;
    margin-top: 12px;
    border: 1px solid #A7F3D0;
    }}

    .aqs-status-error {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-radius: 10px;
    background: #FEF2F2;
    color: #991B1B;
    font-size: .875rem;
    font-weight: 600;
    margin-top: 12px;
    border: 1px solid #FECACA;
    }}

    .aqs-loading-inline {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: .875rem;
    color: #6B7280;
    font-weight: 500;
    margin-top: 12px;
    }}

    .aqs-spinner {{
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid #E5E7EB;
    border-top-color: #2B4EDE;
    border-radius: 50%;
    animation: aqs-spin .7s linear infinite;
    flex-shrink: 0;
    }}

    @keyframes aqs-spin {{
    to {{ transform: rotate(360deg); }}
    }}

    .aqs-empty {{
    font-size: .875rem;
    color: #9CA3AF;
    padding: 12px 0;
    }}

    .aqs-char-count {{
    font-size: .75rem;
    color: #9CA3AF;
    text-align: right;
    margin-top: 4px;
    }}

    @media (max-width: 640px) {{
    .aqs-grid-2 {{
        grid-template-columns: 1fr;
    }}
    }}
    </style>

    <div class="aqs-tabs">
    <button class="aqs-tab active" id="aqs-tab-settings" onclick="aqsTab('settings')">⚙️ Settings</button>
    <button class="aqs-tab" id="aqs-tab-content" onclick="aqsTab('content')">📚 Content Manager</button>
    </div>

    <div id="aqs-panel-settings">
    <form id="aqs-settings-form">
        <div class="aqs-field">
        <label class="aqs-label">Display Name</label>
        <input type="text" name="display_name" class="aqs-input" value="{self.display_name}">
        </div>

        <div class="aqs-field">
        <label class="aqs-label">Default Course ID</label>
        <input type="text" name="course_id" class="aqs-input" value="{self.course_id}">
        <span class="aqs-hint">Fallback course if no learner-selected course is active.</span>
        </div>

        <div class="aqs-field">
        <label class="aqs-label">Backend URL</label>
        <input type="text" name="backend_url" class="aqs-input" value="{self.backend_url}">
        <span class="aqs-hint">Example: http://host.docker.internal:8100</span>
        </div>

        <div class="aqs-field">
        <label class="aqs-label">Questions Per Session</label>
        <input type="number" name="max_questions" class="aqs-input" value="{self.max_questions}" min="1" max="50" style="max-width:110px">
        </div>

        <button type="submit" class="aqs-btn-primary">Save Settings</button>
        <div id="aqs-settings-status"></div>
    </form>
    </div>

    <div id="aqs-panel-content" style="display:none">
    <div class="aqs-toggle">
        <button class="aqs-toggle-btn active" id="aqs-toggle-pdf" onclick="aqsInputMode('pdf')">📄 Upload PDF</button>
        <button class="aqs-toggle-btn" id="aqs-toggle-text" onclick="aqsInputMode('text')">✏️ Paste Text</button>
    </div>

    <div id="aqs-pdf-section">
        <div class="aqs-drop-zone" id="aqs-drop-zone">
        <input type="file" id="aqs-file-input" accept=".pdf" style="display:none">
        <div class="aqs-drop-icon" id="aqs-drop-icon">📄</div>
        <div class="aqs-drop-title" id="aqs-drop-title">Drop a lecture PDF here, or click to browse</div>
        <div class="aqs-drop-sub" id="aqs-drop-sub">Lecture slides or notes only for this phase</div>
        </div>
    </div>

    <div id="aqs-text-section" style="display:none">
        <textarea id="aqs-raw-text" class="aqs-textarea" rows="11" placeholder="Paste lecture text here..."></textarea>
        <div class="aqs-char-count" id="aqs-char-count">0 characters</div>
    </div>

    <div style="margin-top:14px">
        <button class="aqs-btn-primary" id="aqs-btn-parse" onclick="aqsParse()">✨ Parse with AI</button>
    </div>
    <div id="aqs-parse-status"></div>

    <div id="aqs-extracted-section" style="display:none">
        <div class="aqs-divider"><span>Review and Edit Extracted Lecture</span></div>

        <div class="aqs-grid-2">
        <div class="aqs-col-full aqs-field">
            <label class="aqs-label">Lecture Title</label>
            <input type="text" id="aqs-ext-title" class="aqs-input" placeholder="e.g. Relational Schema Mapping">
        </div>

        <div class="aqs-field">
            <label class="aqs-label">Week</label>
            <input type="number" id="aqs-ext-week" class="aqs-input" min="1" max="52" value="1">
        </div>

        <div class="aqs-field">
            <label class="aqs-label">Content Type</label>
            <div style="padding:9px 12px; background:#EEF2FF; border:1.5px solid #C7D2FE; border-radius:10px; font-size:.9rem; font-weight:700; color:#2B4EDE; display:inline-block;">
            📖 Lecture
            </div>
            <span class="aqs-hint">Tutorial and lab support can be added later.</span>
        </div>

        <div class="aqs-col-full aqs-field">
            <label class="aqs-label">Course ID</label>
            <input type="text" id="aqs-ext-course-id" class="aqs-input" value="{self.course_id}">
        </div>

        <div class="aqs-col-full aqs-field">
            <label class="aqs-label">Course Name <span class="aqs-label-hint">— optional</span></label>
            <input type="text" id="aqs-ext-course-name" class="aqs-input" placeholder="e.g. Database Systems">
        </div>
        </div>

        <div class="aqs-field">
        <label class="aqs-label">Topics <span class="aqs-label-hint">— press Enter to add · keep them broad</span></label>
        <div class="aqs-topics-editor" id="aqs-topics-editor" onclick="document.getElementById('aqs-topic-input').focus()">
            <input type="text" id="aqs-topic-input" class="aqs-topic-input" placeholder="Type a topic and press Enter…">
        </div>
        </div>

        <div class="aqs-field">
        <label class="aqs-label">Source Text <span class="aqs-label-hint">— quiz questions will be generated from this</span></label>
        <textarea id="aqs-ext-source" class="aqs-textarea" rows="10" placeholder="Extracted lecture text will appear here..."></textarea>
        <div class="aqs-char-count" id="aqs-source-char-count">0 characters</div>
        </div>

        <div class="aqs-field">
        <label class="aqs-label">AI Summary <span class="aqs-label-hint">— reference only</span></label>
        <textarea id="aqs-ext-summary" class="aqs-textarea" rows="3" readonly></textarea>
        </div>

        <button class="aqs-btn-primary" id="aqs-btn-save" onclick="aqsSaveContent()">💾 Save Lecture</button>
        <div id="aqs-save-status"></div>
    </div>

    <div id="aqs-existing-section">
        <div class="aqs-divider"><span>Saved Lectures</span></div>
        <div id="aqs-existing-list"><p class="aqs-empty">Loading…</p></div>
    </div>
    </div>

    <script>
    (function() {{

    var SAVE_URL = "{save_url}";
    var PARSE_URL = "{parse_url}";
    var SAVE_CONTENT_URL = "{save_content_url}";
    var LIST_CONTENT_URL = "{list_content_url}";

    var currentTopics = [];
    var currentMode = "pdf";
    var selectedFile = null;

    window.aqsTab = function(name) {{
        ["settings", "content"].forEach(function(t) {{
        document.getElementById("aqs-tab-" + t).classList.toggle("active", t === name);
        document.getElementById("aqs-panel-" + t).style.display = t === name ? "" : "none";
        }});
        if (name === "content") aqsLoadExisting();
    }};

    window.aqsInputMode = function(mode) {{
        currentMode = mode;
        document.getElementById("aqs-toggle-pdf").classList.toggle("active", mode === "pdf");
        document.getElementById("aqs-toggle-text").classList.toggle("active", mode === "text");
        document.getElementById("aqs-pdf-section").style.display = mode === "pdf" ? "" : "none";
        document.getElementById("aqs-text-section").style.display = mode === "text" ? "" : "none";
    }};

    var dropZone = document.getElementById("aqs-drop-zone");
    var fileInput = document.getElementById("aqs-file-input");

    dropZone.addEventListener("click", function() {{
        fileInput.click();
    }});

    dropZone.addEventListener("dragover", function(e) {{
        e.preventDefault();
        dropZone.classList.add("drag-over");
    }});

    dropZone.addEventListener("dragleave", function() {{
        dropZone.classList.remove("drag-over");
    }});

    dropZone.addEventListener("drop", function(e) {{
        e.preventDefault();
        dropZone.classList.remove("drag-over");
        var files = e.dataTransfer.files;
        if (files.length > 0 && files[0].type === "application/pdf") {{
        aqsSetFile(files[0]);
        }} else {{
        aqsStatus("aqs-parse-status", "error", "Please drop a valid PDF file.");
        }}
    }});

    fileInput.addEventListener("change", function() {{
        if (fileInput.files.length > 0) aqsSetFile(fileInput.files[0]);
    }});

    function aqsSetFile(file) {{
        selectedFile = file;
        dropZone.classList.add("has-file");
        document.getElementById("aqs-drop-icon").textContent = "✅";
        document.getElementById("aqs-drop-title").textContent = file.name;
        document.getElementById("aqs-drop-sub").textContent = Math.round(file.size / 1024) + " KB — ready to parse";
        aqsStatus("aqs-parse-status", "", "");
    }}

    var rawTextArea = document.getElementById("aqs-raw-text");
    if (rawTextArea) {{
        rawTextArea.addEventListener("input", function() {{
        document.getElementById("aqs-char-count").textContent = rawTextArea.value.length.toLocaleString() + " characters";
        }});
    }}

    var sourceTextArea = document.getElementById("aqs-ext-source");
    if (sourceTextArea) {{
        sourceTextArea.addEventListener("input", function() {{
        document.getElementById("aqs-source-char-count").textContent = sourceTextArea.value.length.toLocaleString() + " characters";
        }});
    }}

    window.aqsParse = function() {{
        var btn = document.getElementById("aqs-btn-parse");

        if (currentMode === "pdf") {{
        if (!selectedFile) {{
            aqsStatus("aqs-parse-status", "error", "Please select a lecture PDF first.");
            return;
        }}

        btn.disabled = true;
        btn.innerHTML = '<span class="aqs-spinner"></span> Reading PDF…';

        var reader = new FileReader();
        reader.onload = function(e) {{
            var base64 = e.target.result.split(",")[1];
            aqsCallParse({{ pdf_base64: base64 }}, btn);
        }};
        reader.onerror = function() {{
            btn.disabled = false;
            btn.innerHTML = "✨ Parse with AI";
            aqsStatus("aqs-parse-status", "error", "Could not read the PDF file.");
        }};
        reader.readAsDataURL(selectedFile);

        }} else {{
        var text = rawTextArea ? rawTextArea.value.trim() : "";
        if (text.length < 50) {{
            aqsStatus("aqs-parse-status", "error", "Please paste more lecture text first.");
            return;
        }}

        btn.disabled = true;
        btn.innerHTML = '<span class="aqs-spinner"></span> Analysing with AI…';
        aqsCallParse({{ raw_text: text }}, btn);
        }}
    }};

    function aqsCallParse(payload, btn) {{
        aqsStatus("aqs-parse-status", "loading", "Extracting lecture structure and topics — this may take a moment…");

        jQuery.ajax({{
        type: "POST",
        url: PARSE_URL,
        data: JSON.stringify(payload),
        contentType: "application/json",
        timeout: 90000,
        success: function(data) {{
            btn.disabled = false;
            btn.innerHTML = "✨ Parse with AI";

            if (data.success && data.extracted) {{
            aqsPopulateExtracted(data.extracted);
            aqsStatus("aqs-parse-status", "success", "Lecture extracted successfully. Review and edit below.");
            }} else {{
            aqsStatus("aqs-parse-status", "error", data.error || "Extraction failed.");
            }}
        }},
        error: function(xhr) {{
            btn.disabled = false;
            btn.innerHTML = "✨ Parse with AI";

            var detail = "Extraction failed.";
            try {{
            var parsed = JSON.parse(xhr.responseText);
            detail = parsed.detail || detail;
            }} catch (e) {{}}

            aqsStatus("aqs-parse-status", "error", detail);
        }}
        }});
    }}

    function aqsPopulateExtracted(ext) {{
        document.getElementById("aqs-ext-title").value = ext.suggested_title || "";
        document.getElementById("aqs-ext-week").value = ext.suggested_week || 1;
        document.getElementById("aqs-ext-source").value = ext.source_text || "";
        document.getElementById("aqs-ext-summary").value = ext.summary || "";

        document.getElementById("aqs-source-char-count").textContent =
        (ext.source_text || "").length.toLocaleString() + " characters";

        currentTopics = Array.isArray(ext.topics) ? ext.topics.slice() : [];
        aqsRenderTopics();

        var section = document.getElementById("aqs-extracted-section");
        section.style.display = "";
        setTimeout(function() {{
        section.scrollIntoView({{ behavior: "smooth", block: "start" }});
        }}, 100);
    }}

    function aqsRenderTopics() {{
        var editor = document.getElementById("aqs-topics-editor");
        var topicInput = document.getElementById("aqs-topic-input");

        editor.innerHTML = "";

        currentTopics.forEach(function(topic, i) {{
        var tag = document.createElement("span");
        tag.className = "aqs-topic-tag";
        tag.innerHTML = aqsEscape(topic) +
            '<button class="aqs-topic-remove" title="Remove" onclick="aqsRemoveTopic(' + i + ')">×</button>';
        editor.appendChild(tag);
        }});

        editor.appendChild(topicInput);
        topicInput.focus();
    }}

    window.aqsRemoveTopic = function(i) {{
        currentTopics.splice(i, 1);
        aqsRenderTopics();
    }};

    document.addEventListener("keydown", function(e) {{
        var topicInput = document.getElementById("aqs-topic-input");
        if (!topicInput || document.activeElement !== topicInput) return;

        if (e.key === "Enter") {{
        e.preventDefault();
        var val = topicInput.value.trim();
        if (val && currentTopics.indexOf(val) === -1) {{
            currentTopics.push(val);
            topicInput.value = "";
            aqsRenderTopics();
        }}
        }}
    }});

    window.aqsSaveContent = function() {{
        var title = document.getElementById("aqs-ext-title").value.trim();
        var week = parseInt(document.getElementById("aqs-ext-week").value || "1", 10);
        var courseId = document.getElementById("aqs-ext-course-id").value.trim();
        var courseName = document.getElementById("aqs-ext-course-name").value.trim();
        var sourceText = document.getElementById("aqs-ext-source").value.trim();
        var ctype = "lecture";

        if (!title) {{
        aqsStatus("aqs-save-status", "error", "Please enter a lecture title.");
        return;
        }}
        if (!courseId) {{
        aqsStatus("aqs-save-status", "error", "Please enter a Course ID.");
        return;
        }}
        if (currentTopics.length === 0) {{
        aqsStatus("aqs-save-status", "error", "Please add at least one topic.");
        return;
        }}
        if (sourceText.length < 50) {{
        aqsStatus("aqs-save-status", "error", "Source text is too short.");
        return;
        }}

        var saveBtn = document.getElementById("aqs-btn-save");
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<span class="aqs-spinner"></span> Saving…';

        jQuery.ajax({{
        type: "POST",
        url: SAVE_CONTENT_URL,
        data: JSON.stringify({{
            course_id: courseId,
            course_name: courseName || null,
            title: title,
            week: week,
            content_type: ctype,
            topics: currentTopics,
            source_text: sourceText,
            active: true
        }}),
        contentType: "application/json",
        success: function(data) {{
            saveBtn.disabled = false;
            saveBtn.innerHTML = "💾 Save Lecture";

            if (data.success) {{
    var settingsCourseField = document.querySelector('#aqs-settings-form input[name="course_id"]');
    if (settingsCourseField) {{
        settingsCourseField.value = courseId;
    }}

    var extractedCourseField = document.getElementById("aqs-ext-course-id");
    if (extractedCourseField) {{
        extractedCourseField.value = courseId;
    }}

    aqsStatus("aqs-save-status", "success", "Lecture saved successfully.");
    document.getElementById("aqs-extracted-section").style.display = "none";
    aqsResetInput();
    aqsLoadExisting();
}} else {{
            aqsStatus("aqs-save-status", "error", data.error || "Save failed.");
            }}
        }},
        error: function() {{
            saveBtn.disabled = false;
            saveBtn.innerHTML = "💾 Save Lecture";
            aqsStatus("aqs-save-status", "error", "Network error — could not save lecture.");
        }}
        }});
    }};

    function aqsLoadExisting() {{
    var list = document.getElementById("aqs-existing-list");
    list.innerHTML = '<p class="aqs-empty"><span class="aqs-spinner"></span> Loading…</p>';

    var currentCourseId =
        (document.getElementById("aqs-ext-course-id") && document.getElementById("aqs-ext-course-id").value.trim()) ||
        (document.querySelector('#aqs-settings-form input[name="course_id"]') && document.querySelector('#aqs-settings-form input[name="course_id"]').value.trim()) ||
        "{self.course_id}";

    jQuery.ajax({{
        type: "POST",
        url: LIST_CONTENT_URL,
        data: JSON.stringify({{
            course_id: currentCourseId
        }}),
        contentType: "application/json",
        success: function(data) {{
            if (!data.success || !data.items || data.items.length === 0) {{
                list.innerHTML = '<p class="aqs-empty">No saved lectures yet for course ' + aqsEscape(currentCourseId) + '.</p>';
                return;
            }}

            var html = "";
            data.items.forEach(function(item) {{
                html +=
                    '<div class="aqs-content-item">' +
                        '<div>' +
                            '<div class="aqs-content-item-title">' + aqsEscape(item.title) + '</div>' +
                            '<div class="aqs-content-item-meta">' +
                                'Week ' + (item.week || "?") + ' · ' +
                                ((item.topics || []).length) + ' topic' + (((item.topics || []).length) === 1 ? '' : 's') +
                            '</div>' +
                        '</div>' +
                        '<span class="aqs-type-badge aqs-type-lecture">Lecture</span>' +
                    '</div>';
            }});

            list.innerHTML = html;
        }},
        error: function() {{
            list.innerHTML = '<p class="aqs-empty">Could not load saved lectures.</p>';
        }}
    }});
}}

    document.getElementById("aqs-settings-form").addEventListener("submit", function(e) {{
        e.preventDefault();

        var data = {{}};
        new FormData(this).forEach(function(v, k) {{
        data[k] = v;
        }});
        data.max_questions = parseInt(data.max_questions, 10);

        jQuery.ajax({{
        type: "POST",
        url: SAVE_URL,
        data: JSON.stringify(data),
        contentType: "application/json",
        success: function() {{
            aqsStatus("aqs-settings-status", "success", "Settings saved.");
        }},
        error: function() {{
            aqsStatus("aqs-settings-status", "error", "Failed to save settings.");
        }}
        }});
    }});

    function aqsResetInput() {{
        selectedFile = null;
        dropZone.classList.remove("has-file");
        document.getElementById("aqs-drop-icon").textContent = "📄";
        document.getElementById("aqs-drop-title").textContent = "Drop a lecture PDF here, or click to browse";
        document.getElementById("aqs-drop-sub").textContent = "Lecture slides or notes only for this phase";
        if (rawTextArea) rawTextArea.value = "";
        document.getElementById("aqs-char-count").textContent = "0 characters";
        currentTopics = [];
        aqsStatus("aqs-parse-status", "", "");
        aqsStatus("aqs-save-status", "", "");
    }}

    function aqsStatus(id, type, msg) {{
        var el = document.getElementById(id);
        if (!el) return;

        if (!type || !msg) {{
        el.innerHTML = "";
        return;
        }}

        if (type === "loading") {{
        el.innerHTML = '<div class="aqs-loading-inline"><span class="aqs-spinner"></span>' + aqsEscape(msg) + '</div>';
        }} else {{
        var cls = type === "success" ? "aqs-status-success" : "aqs-status-error";
        el.innerHTML = '<div class="' + cls + '">' + aqsEscape(msg) + '</div>';
        }}
    }}

    function aqsEscape(s) {{
        return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    aqsLoadExisting();

    }})();
    </script>
    </div>
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
    def parse_pdf(self, data, suffix=""):
            """
            Proxy PDF/text to backend parser for Studio content ingestion.
            """
            payload = {}
            if data.get("pdf_base64"):
                payload["pdf_base64"] = data["pdf_base64"]
            elif data.get("raw_text"):
                payload["raw_text"] = data["raw_text"]
            else:
                return {"success": False, "error": "No content provided."}

            resp = self._api("/api/quiz/content/parse", payload=payload, timeout=90)

            if resp and resp.get("success"):
                return {"success": True, "extracted": resp.get("extracted", {})}
            return {"success": False, "error": "Content extraction failed. Check backend logs."}

    @XBlock.json_handler
    def save_content_item(self, data, suffix=""):
        """
        Save reviewed lecture content to MongoDB.
        """
        required = ["course_id", "title", "week", "topics", "source_text"]
        for field in required:
            if not data.get(field):
                return {"success": False, "error": f"Missing required field: {field}"}

        payload = {
            "course_id": data["course_id"],
            "course_name": data.get("course_name"),
            "week": int(data["week"]),
            "content_type": "lecture",
            "title": data["title"],
            "topics": data["topics"],
            "source_text": data["source_text"],
            "active": True,
        }

        resp = self._api("/api/quiz/content/add", payload=payload)
        if resp and resp.get("success"):
            return {"success": True, "message": resp.get("message", "Content saved.")}
        return {"success": False, "error": "Failed to save content item."}

    @XBlock.json_handler
    def list_content_studio(self, data, suffix=""):
        """
        Return saved lecture items for Studio content manager.
        """
        course_id = data.get("course_id") or self.course_id
        resp = self._api(f"/api/quiz/content/{course_id}", method="GET")
        if resp:
            return {"success": True, "items": resp.get("items", [])}
        return {"success": True, "items": []}
    
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