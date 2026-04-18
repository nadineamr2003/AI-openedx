from pydantic import BaseModel, Field, field_validator


class GenerateRequest(BaseModel):
    student_id:    str
    course_id:     str
    topic:         str
    difficulty:    int = Field(default=3, ge=1, le=5)
    source_text:   str
    mode:          str = "normal_practice"
    session_origin: str | None = None
    content_ids:   list[str] = []
    focus_topics:  list[str] | None = None
    question_count: int = 10


class SubmitRequest(BaseModel):
    student_id:       str
    course_id:        str
    question_id:      str
    question_text:    str | None = None
    options:          dict[str, str] | None = None
    explanation:      str | None = None
    selected_answer:  str
    correct_answer:   str
    topic:            str
    difficulty:       int
    time_spent_ms:    int
    time_context:     str | None = None
    confidence:       str | None = None
    session_id:       str | None = None


class SubmitResponse(BaseModel):
    is_correct:                      bool
    explanation:                     str
    updated_mastery:                 float
    next_difficulty:                 int
    next_topic:                      str
    next_mode:                       str
    support_features:                list[str]
    session_complete:                bool
    session_recommendation:          str | None = None
    recommendation_code:            str | None = None
    recommendation_title:           str | None = None
    recommendation_text:            str | None = None
    weakest_topic_this_session:      str | None = None
    strongest_topic_this_session:    str | None = None
    session_accuracy:                float | None = None
    avg_time_spent_ms:               int | None = None
    lectures_practised_count:        int | None = None
    topics_practised_count:          int | None = None
    content_mastery_summaries:       list[dict] | None = None
    recommended_review_topic:        str | None = None
    recommended_review_topics:       list[str] | None = None
    selected_content_ids:            list[str] | None = None
    course_id:                       str | None = None
    session_origin:                  str | None = None
    focused_topic_mastery_summary:   dict | None = None
    followup_topics_practised:       list[str] | None = None
    followup_topic_mastery_summaries: list[dict] | None = None
    narrative_bridge:                str | None = None
    recovery_step_available:         bool = False
    recovery_message:                str | None = None
    recovery_reason:                 str | None = None
    recovery_topic:                  str | None = None


class RecoveryStartRequest(BaseModel):
    student_id: str
    course_id: str
    session_id: str
    question_id: str | None = None
    question_text: str
    explanation: str
    topic: str
    difficulty: int = Field(default=3, ge=1, le=5)
    source_text: str


class RecoveryDeclineRequest(BaseModel):
    student_id: str
    course_id: str
    session_id: str
    topic: str


class RecoverySubmitRequest(BaseModel):
    student_id: str
    course_id: str
    session_id: str
    question_id: str
    question_text: str | None = None
    options: dict[str, str] | None = None
    explanation: str | None = None
    selected_answer: str
    correct_answer: str
    topic: str
    difficulty: int
    time_spent_ms: int
    time_context: str | None = None
    confidence: str | None = None
    recovery_for_topic: str | None = None


class MasteryResponse(BaseModel):
    student_id:    str
    course_id:     str
    topic_mastery: dict[str, float]
    topic_labels:  dict[str, str]
    weak_topics:   list[str]
    strong_topics: list[str]


class ContentItem(BaseModel):
    course_id:    str
    course_name:  str
    week:         int
    content_type: str
    title:        str
    topics:       list[str]
    source_text:  str
    active:       bool = True
    require_reassessment: bool = False

    @field_validator("course_name")
    @classmethod
    def validate_course_name(cls, value: str) -> str:
        course_name = str(value or "").strip()
        if not course_name:
            raise ValueError("course_name is required")
        return course_name


class ContentListResponse(BaseModel):
    course_id: str
    items:     list[dict]


class ContentUpdateRequest(BaseModel):
    content_id:   str
    course_id:    str
    course_name:  str
    week:         int
    title:        str
    topics:       list[str]
    source_text:  str
    active:       bool = True
    require_reassessment: bool = False

    @field_validator("course_name")
    @classmethod
    def validate_course_name(cls, value: str) -> str:
        course_name = str(value or "").strip()
        if not course_name:
            raise ValueError("course_name is required")
        return course_name


class ContentToggleRequest(BaseModel):
    content_id: str
    active:     bool


# ── Diagnostic models ────────────────────────────────────────────────

class DiagnosticResult(BaseModel):
    difficulty: int
    correct: bool
    time_ms: int
    topic: str


class DiagnosticItem(BaseModel):
    """One content item returned by session/start when diagnostic is needed."""
    content_id: str
    title: str
    topics: list[str]
    source_text: str
    source_version: str
    diagnostic_target_questions: int
    diagnostic_coverage_goal: int


class DiagnosticGenerateRequest(BaseModel):
    student_id: str
    course_id: str
    topic: str | None = None
    question_index: int = Field(ge=0, le=20)
    topics: list[str] = []
    source_text: str
    results_so_far: list[DiagnosticResult] = []
    target_questions: int | None = None


class DiagnosticCompleteRequest(BaseModel):
    student_id: str
    course_id: str
    content_id: str
    source_version: str
    topics: list[str]
    results: list[DiagnosticResult]


class SessionFinalizeRequest(BaseModel):
    student_id: str
    course_id: str
    content_ids: list[str]
    question_count: int = 10
    mode: str = "normal_practice"
    session_origin: str | None = None
    focus_topics: list[str] | None = None


class SessionResumeRequest(BaseModel):
    student_id: str
    course_id: str
    session_id: str


class SessionRetireRequest(BaseModel):
    student_id: str
    course_id: str
    session_id: str
