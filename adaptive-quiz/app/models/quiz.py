from pydantic import BaseModel

class GenerateRequest(BaseModel):
    student_id: str
    course_id: str
    topic: str
    difficulty: int = 2       # 1=easy 2=medium 3=hard
    source_text: str
    mode: str = "auto"        # auto | weakness_review | challenge
    content_ids: list[str] = []

class SubmitRequest(BaseModel):
    student_id: str
    course_id: str
    question_id: str
    selected_answer: str
    correct_answer: str
    topic: str
    difficulty: int
    time_spent_ms: int

class SubmitResponse(BaseModel):
    is_correct: bool
    explanation: str
    updated_mastery: float
    next_difficulty: int
    next_topic: str
    next_mode: str
    support_features: list[str]
    session_complete: bool

class MasteryResponse(BaseModel):
    student_id: str
    course_id: str
    topic_mastery: dict[str, float]
    weak_topics: list[str]
    strong_topics: list[str]

class ContentItem(BaseModel):
    course_id: str
    week: int
    content_type: str        # lecture | tutorial | lab 
    title: str
    topics: list[str]
    source_text: str
    active: bool = True

class ContentListResponse(BaseModel):
    course_id: str
    items: list[dict]