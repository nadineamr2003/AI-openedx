import httpx
import os
import json
import logging
import re
import time
from dotenv import load_dotenv
import random
from typing import Any, Dict, List, Optional

load_dotenv()

logger = logging.getLogger(__name__)

# =========================
# API KEYS
# =========================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")

# =========================
# HELPERS
# =========================
def _csv_env(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]

PROVIDER_ORDER = _csv_env(
    "PROVIDER_ORDER",
    ["gemini", "groq", "cerebras", "openrouter", "huggingface"]
)

# Keep provider model lists in env so you can tune them without editing code.
PROVIDER_MODELS = {
    "gemini": _csv_env("GEMINI_MODELS", ["gemini-3-flash-preview"]),
    "groq": _csv_env("GROQ_MODELS", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]),
    "cerebras": _csv_env("CEREBRAS_MODELS", ["llama3.1-8b", "gpt-oss-120b"]),
    "openrouter": _csv_env(
        "OPENROUTER_MODELS",
        [
            "z-ai/glm-4.5-air:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemma-3-4b-it:free",
            "openrouter/free",
        ],
    ),
    # Example syntax shown by HF docs. Replace with another available model/provider if needed.
    "huggingface": _csv_env("HF_MODELS", ["meta-llama/Llama-3.1-8B-Instruct:cerebras"]),
}

PROVIDERS = {
    "gemini": {
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "api_key": GEMINI_API_KEY,
    },
    "groq": {
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": GROQ_API_KEY,
    },
    "cerebras": {
        "endpoint": "https://api.cerebras.ai/v1/chat/completions",
        "api_key": CEREBRAS_API_KEY,
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "api_key": OPENROUTER_API_KEY,
    },
    "huggingface": {
        "endpoint": "https://router.huggingface.co/v1/chat/completions",
        "api_key": HF_TOKEN,
    },
}

OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "").strip()
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "").strip()
_PROVIDER_COOLDOWNS: dict[str, float] = {}
_PROVIDER_503_FAILURES: dict[str, list[float]] = {}

VERY_EASY_EASY_ANGLES = [
    "Focus on the definition or core concept.",
    "Focus on a direct comparison between related concepts.",
    "Focus on a simple process step or sequence.",
    "Focus on a straightforward practical use or example.",
]

MEDIUM_ANGLES = [
    "Focus on a practical use case or real example that needs one step of application.",
    "Focus on a direct consequence of applying the concept.",
    "Focus on what happens when this concept is applied incorrectly in a simple case.",
    "Focus on choosing the best interpretation of a short, concrete situation.",
]

HARD_ANGLES = [
    "Focus on choosing the best explanation among close alternatives.",
    "Focus on distinguishing two easily confused concepts in a non-obvious way.",
    "Focus on interpreting a short scenario or symptom rather than recalling a sentence.",
    "Focus on a tradeoff or consequence under a meaningful constraint.",
]

VERY_HARD_ANGLES = [
    "Focus on a tradeoff under meaningful constraints and competing priorities.",
    "Focus on diagnosing a failure, symptom, or consequence from multiple plausible causes.",
    "Focus on the best explanation among very close alternatives with subtle distinctions.",
    "Focus on an edge case, exception, or subtle distinction that changes the best answer.",
    "Focus on a scenario with competing priorities where multiple answers seem plausible at first.",
]

MAX_DIRECT_SOURCE_CHARS = 12000
TARGET_CHUNK_SIZE = 1200
MAX_CHUNK_SIZE = 1500
MIN_CONTEXT_CHARS = 8000
MAX_CONTEXT_CHARS = 10000
MAX_TOPIC_CHUNKS = 4

TOPIC_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "based", "by", "for", "from",
    "in", "into", "is", "of", "on", "or", "the", "to", "using", "via",
    "with",
}

DEFINITION_MARKERS = [
    "is defined as",
    "refers to",
    "means",
    "defined as",
    "is the process",
    "is a process",
    "is the ability",
]

EXPLANATION_MARKERS = [
    "because",
    "therefore",
    "for example",
    "for instance",
    "such as",
]

PROMPT_TEMPLATE = """
Generate one multiple-choice question based ONLY on the provided text.
Requirements:
- Be creative with the question angle — avoid repeating common phrasings
- Question angle: {variation}
- Topic: {topic}
- Difficulty: {difficulty_label}

Difficulty contract:
- very easy = single-fact recognition or obvious cue from the source
- easy = direct recall, direct definition, or one-step identification
- medium = one-step application, direct comparison, or simple consequence
- hard = multi-clue reasoning, close comparison, short applied scenario, or best-answer selection
- very hard = nuanced distinction, tradeoff, best explanation, edge case, or tricky application

Additional difficulty rules:
- Very easy and easy questions must NOT require multi-step reasoning.
- Hard and very hard questions must NOT be solvable by spotting one obvious keyword.
- Hard and very hard questions should require reasoning, discrimination, or applied interpretation.
- Hard and very hard questions should not be direct lecture restatements with slightly more formal wording.
- "Why" wording by itself does NOT make a question hard.
- If the answer is stated directly in the source, that usually fits medium or hard unless the question adds a genuine reasoning layer.
- Very hard questions should require at least one of: nuanced distinction, tradeoff, edge case, best explanation among close alternatives, or short scenario-based interpretation.
- If the topic is introductory and the source is direct, prefer an honest medium or hard question over a fake very-hard one.
- Do NOT introduce unsupported assumptions into the stem or options.
- Do NOT invent numeric thresholds, timing values, protocol mechanisms, or system behaviors unless they are clearly supported by the source text.
- If you use a scenario, keep it neutral unless the extra details are directly grounded in the source.
- Do not make a question hard just by wrapping a directly stated fact in a formal scenario.
- For TCP/UDP or similar direct contrasts, do NOT write a very-hard question that merely rephrases which option is more suitable or why, unless the scenario introduces a real tradeoff, constraint, or ambiguity.
- Very hard questions should not be simple source paraphrases with stronger wording.
- If the source supports only a direct concept contrast, generate an honest medium or hard question instead of a fake very-hard one.
- If the source text is too shallow to support the requested difficulty honestly, generate the fairest strong question possible without inventing unsupported depth.
- Do not fake difficulty with vague wording, unnecessary complexity, or fancy phrasing.
- Prefer clear but intellectually honest difficulty.

Question quality rules:
- The "question" field must contain ONLY the question stem.
- Do NOT include answer choices, answer labels, or option text inside the question stem.
- Do NOT write A), B), C), D), A., B., C., or D. inside the question stem.
- Put answer choices only inside the "options" object.
- Exactly 4 answer choices labeled A, B, C, D
- Exactly 1 correct answer
- Randomize which answer choice is correct. It must not systematically be A.
- A short explanation (2-3 sentences) for why the correct answer is right
- Do not mention answer letters in the explanation. Explain using the concept/content itself.
- Stay grounded in the provided material — do NOT invent facts
- All distractors must be plausible
- Distractors must differ for meaningful conceptual reasons, not trivial wording tricks
- Distractors must be incorrect or less accurate than the correct answer, not alternative true statements.
- Hard and very hard distractors should be close and credible, not obviously wrong
- Hard and very hard distractors may be partly true in another context, but must be less correct than the right answer in this specific context.
- Do not use silly, unrelated, or trivially eliminable distractors for hard or very hard questions.
- Hard and very hard distractors should stay plausible within the same domain.
- Hard and very hard distractors should not introduce random unrelated mechanisms or obviously false protocol claims.
- Prefer concept understanding, application, comparison, consequence, best explanation, or careful distinction over wording tricks
- Avoid vague meta phrasing like "according to the text" or "in page" unless absolutely necessary.
- The correct answer must be clearly supported by the source text.
- Hard and very hard questions should feel like genuine reasoning questions rather than plain recall with harder wording.
- Easy and very easy questions should feel honestly direct rather than accidentally tricky.
- Ignore instructor names, staff names, office hours, course logistics, grades, contact details, URLs, and administrative information
- Never ask about who teaches the course, staff members, email addresses, office hours, access codes, or grade distribution
- Do NOT generate misconception-style stems such as "What is a misconception..." or "Which wrong assumption..."
- If the source text is too weak to support a high-quality question on this topic, choose a safer factual angle rather than inventing nuance.

Source text:
{source_text}

Respond ONLY with this JSON. No preamble, no markdown fences, no extra text:
{{
  "question": "...",
  "options": {{"A": "...", "B": "...", "C": "...", "D": "..." }},
  "correct_answer": "A",
  "explanation": "...",
  "topic": "{topic}",
  "difficulty": {difficulty}
}}
"""

CONTENT_EXTRACTION_PROMPT = """
You are helping an instructor organize university lecture material for a quiz system.

You will receive extracted lecture text from a lecture PDF.
Your job is to return METADATA ONLY.

Return ONLY this JSON:
{{
  "suggested_title": "...",
  "suggested_week": 1,
  "topics": ["topic1", "topic2", "topic3", "topic4"],
  "summary": "..."
}}

Rules:
- suggested_title:
  - must be formal, descriptive, and course-appropriate
  - do NOT abbreviate casually
  - do NOT use vague titles like "Introduction", "Week 1", or "Overview"
  - if the lecture clearly has two major themes, include both in the title
  - bad: "Intro to SW Eng"
  - good: "Introduction to Software Engineering and Requirements Engineering"

- suggested_week:
  - integer
  - infer from lecture headers if possible
  - otherwise return 1

- topics:
  - extract 4 to 6 BROAD lecture concepts
  - topics must reflect the MAIN LEARNING CONTENT across the FULL sampled lecture, not just the opening slides
  - prefer concept-level topics, not keywords, commands, names, or slide labels
  - ignore instructor names, staff names, emails, logistics, office hours, grades, resources, memes, and self-study/admin material
  - bad: "Software Types", "Intro", "Dr. Mervat", "Course Resources"
  - good: "Software Engineering Foundations", "Requirements Engineering Fundamentals", "Requirements Types", "Requirements Engineering Process"

- summary:
  - 2 to 3 factual sentences
  - summarize the main learning content across the full lecture
  - do not focus only on the first section if later sections are substantial
  - ignore instructor/staff/admin/logistics material

Important:
- Do NOT return source_text
- Do NOT paraphrase the lecture text itself
- Do NOT invent topics not clearly supported by the text

Lecture text:
{text}
"""

class ProviderHTTPError(Exception):
    def __init__(self, provider: str, model: str, status_code: int, message: str):
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.message = message
        super().__init__(f"{provider}/{model} -> {status_code}: {message}")


class NoValidQuestionError(ValueError):
    pass


def validate_question(q: dict, source_text: str | None = None) -> tuple[bool, list[str]]:
    reasons = []
    required = ["question", "options", "correct_answer", "explanation", "topic", "difficulty"]
    if not all(k in q for k in required):
        reasons.append("missing_required_fields")
        return False, reasons
    if not isinstance(q["options"], dict):
        reasons.append("options_not_dict")
        return False, reasons
    if q["correct_answer"] not in q["options"]:
        reasons.append("correct_answer_not_in_options")
        return False, reasons
    if len(q["options"]) != 4:
        reasons.append("wrong_option_count")
    if len(q["question"].strip()) < 10:
        reasons.append("question_too_short")
    if _question_contains_embedded_options(q["question"]):
        reasons.append("embedded_options")
    if _looks_like_admin_question(q):
        reasons.append("admin_question")
    if _looks_like_misframed_misconception_question(q):
        reasons.append("misframed_misconception")
    reasons.extend(_difficulty_mismatch_reasons(q, source_text))
    return len(reasons) == 0, reasons

def validate_question_core_only(q: dict) -> tuple[bool, list[str]]:
    reasons = []
    required = ["question", "options", "correct_answer", "explanation", "topic", "difficulty"]

    if not all(k in q for k in required):
        reasons.append("missing_required_fields")
        return False, reasons

    if not isinstance(q["options"], dict):
        reasons.append("options_not_dict")
        return False, reasons

    if q["correct_answer"] not in q["options"]:
        reasons.append("correct_answer_not_in_options")

    if len(q["options"]) != 4:
        reasons.append("wrong_option_count")

    if len(str(q["question"]).strip()) < 10:
        reasons.append("question_too_short")

    if _question_contains_embedded_options(str(q.get("question", ""))):
        reasons.append("embedded_options")

    if _looks_like_admin_question(q):
        reasons.append("admin_question")

    if _looks_like_misframed_misconception_question(q):
        reasons.append("misframed_misconception")

    return len(reasons) == 0, reasons

MISCONCEPTION_STEM_MARKERS = [
    "misconception",
    "common misconception",
    "mistaken belief",
    "incorrect belief",
    "wrong assumption",
]

def _looks_like_misframed_misconception_question(q: dict) -> bool:
    question_text = str(q.get("question", "")).lower()
    return any(marker in question_text for marker in MISCONCEPTION_STEM_MARKERS)


def _question_contains_embedded_options(question_text: str) -> bool:
    if not question_text:
        return False

    markers = re.findall(r"(?:^|[\s(\[{])([A-D])[\)\.](?=\s+\S)", question_text)
    return len(markers) >= 2

def _remap_explanation_letter(explanation: str, old_letter: str, new_letter: str) -> str:
    if not explanation or old_letter == new_letter:
        return explanation

    replacements = [
        (f"Option {old_letter}", f"Option {new_letter}"),
        (f"option {old_letter}", f"option {new_letter}"),
        (f"Choice {old_letter}", f"Choice {new_letter}"),
        (f"choice {old_letter}", f"choice {new_letter}"),
        (f"Answer {old_letter}", f"Answer {new_letter}"),
        (f"answer {old_letter}", f"answer {new_letter}"),
        (f"({old_letter})", f"({new_letter})"),
    ]

    for src, dst in replacements:
        explanation = explanation.replace(src, dst)

    return explanation


def _shuffle_question_options(q: dict) -> dict:
    letters = ["A", "B", "C", "D"]
    original_options = q["options"]
    original_correct = q["correct_answer"]

    pairs = list(original_options.items())
    random.shuffle(pairs)

    new_options = {}
    new_correct = None

    for new_letter, (old_letter, text) in zip(letters, pairs):
        new_options[new_letter] = text
        if old_letter == original_correct:
            new_correct = new_letter

    q["options"] = new_options
    q["correct_answer"] = new_correct
    q["explanation"] = _remap_explanation_letter(
        q.get("explanation", ""),
        original_correct,
        new_correct
    )

    return q


def _build_headers(provider_name: str, api_key: str) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider_name == "openrouter":
        if OPENROUTER_SITE_URL:
            headers["HTTP-Referer"] = OPENROUTER_SITE_URL
        if OPENROUTER_APP_NAME:
            headers["X-OpenRouter-Title"] = OPENROUTER_APP_NAME
    return headers


def _extract_error_message(data: Any) -> str:
    if isinstance(data, dict):
        # OpenAI-compatible style
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("message", "code", "type"):
                if err.get(key):
                    return str(err[key])
            return json.dumps(err)

        if isinstance(err, str):
            return err

        # Some providers use other keys
        for key in ("message", "detail", "error_message"):
            if data.get(key):
                return str(data[key])

    return "Unknown error"

def _response_excerpt(resp: httpx.Response, max_len: int = 300) -> str:
    try:
        text = resp.text.strip()
        if not text:
            return "<empty body>"
        return text[:max_len]
    except Exception:
        return "<unreadable body>"


def _safe_exception_text(e: Exception) -> str:
    text = repr(e)
    return text if len(text) <= 300 else text[:300] + "..."


def _provider_cooldown_remaining(provider_name: str) -> float:
    now = time.time()
    expires_at = _PROVIDER_COOLDOWNS.get(provider_name, 0.0)
    if expires_at <= now:
        _PROVIDER_COOLDOWNS.pop(provider_name, None)
        return 0.0
    return expires_at - now


def _set_provider_cooldown(provider_name: str, seconds: int) -> None:
    _PROVIDER_COOLDOWNS[provider_name] = time.time() + seconds


def _record_provider_throttle(provider_name: str, status_code: int) -> None:
    if provider_name != "gemini":
        return

    now = time.time()
    if status_code == 429:
        _set_provider_cooldown(provider_name, 600)
        return

    if status_code != 503:
        return

    failures = _PROVIDER_503_FAILURES.get(provider_name, [])
    failures = [stamp for stamp in failures if now - stamp <= 120]
    failures.append(now)
    _PROVIDER_503_FAILURES[provider_name] = failures
    if len(failures) >= 2:
        _set_provider_cooldown(provider_name, 120)


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429, 500, 502, 503, 504}


def _strip_markdown_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
    return raw.strip()


def _extract_message_content(data: dict) -> str:
    # Typical OpenAI-compatible shape
    content = data["choices"][0]["message"]["content"]

    # Usually string, but some providers may return structured content.
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    text_parts.append(item["text"])
                elif "content" in item and isinstance(item["content"], str):
                    text_parts.append(item["content"])
        return "\n".join(text_parts).strip()

    return str(content).strip()


async def _call_model(
    client: httpx.AsyncClient,
    provider_name: str,
    model: str,
    prompt: str,
) -> str:
    provider = PROVIDERS[provider_name]
    api_key = provider["api_key"]
    endpoint = provider["endpoint"]

    if not api_key:
        raise RuntimeError(f"Missing API key for provider: {provider_name}")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }

    headers = _build_headers(provider_name, api_key)

    logger.info(f"[LLM] Request -> provider={provider_name} model={model}")

    resp = await client.post(endpoint, headers=headers, json=payload)

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code >= 400:
        msg = _extract_error_message(data)
        excerpt = _response_excerpt(resp)
        raise ProviderHTTPError(
            provider=provider_name,
            model=model,
            status_code=resp.status_code,
            message=f"{msg} | body={excerpt}",
        )

    content = _extract_message_content(data)
    logger.info(f"[LLM] Response OK <- provider={provider_name} model={model}")
    return content

ADMIN_MARKERS = [
    "communication rules",
    "office hours",
    "piazza",
    "access code",
    "course resources",
    "course grade distribution",
    "final exam",
    "midterm exam",
    "thank you",
    "weekly dilbert",
    "self study",
    "exercise",
]

FORBIDDEN_QUESTION_MARKERS = [
    "office hours",
    "piazza",
    "access code",
    "email",
    "course grade",
    "final exam",
    "midterm exam",
    "project",
    "thank you",
]

REASONING_MARKERS = [
    "best explanation",
    "best describes",
    "best answer",
    "most likely",
    "why",
    "how would",
    "what happens if",
    "what would happen if",
    "which outcome",
    "which scenario",
    "tradeoff",
    "compared with",
    "compared to",
    "difference between",
    "distinguish",
    "scenario",
]

PLAIN_RECALL_MARKERS = [
    "what is",
    "which is",
    "which term",
    "which statement defines",
    "what does",
    "which of the following is the definition",
    "what term describes",
]

HIGH_DIFFICULTY_REASONING_MARKERS = [
    "best explanation",
    "best accounts for",
    "most likely",
    "tradeoff",
    "constraint",
    "edge case",
    "in a scenario",
    "in this scenario",
    "suppose",
    "consider",
    "under which",
    "under a",
    "given that",
    "despite",
    "even if",
]

DIRECT_RESTATEMENT_PREFIXES = [
    "what is the primary reason",
    "what is the main reason",
    "what is the primary purpose",
    "why is ",
    "why are ",
    "why does ",
    "why do ",
    "which statement best describes",
    "which statement correctly describes",
    "which statement is true about",
    "what is true about",
]

WEAK_DISTRACTOR_MARKERS = [
    "all of the above",
    "none of the above",
]

GENERIC_QUESTION_TERMS = {
    "about", "against", "among", "because", "beneficial", "best", "better",
    "cause", "causes", "choice", "close", "compared", "comparison", "condition",
    "consequence", "constraint", "correct", "delivery", "describes", "difference",
    "effect", "exact", "explains", "factor", "feature", "following", "given",
    "happens", "important", "likely", "main", "matters", "mechanism", "most",
    "option", "over", "preferred", "priority", "protocol", "question", "reason",
    "result", "scenario", "situation", "specific", "statement", "suitable",
    "suitability", "supports", "system", "timely", "under", "using", "versus",
    "which", "while", "would",
}

DOMAIN_GENERAL_ALLOWED_TERMS = {
    "application", "applications", "behavior", "behaviors", "case", "cases",
    "client", "clients", "communication", "communications", "connection",
    "connections", "context", "contexts", "data", "delay", "delays", "flow",
    "flows", "latency", "media", "message", "messages", "network", "networks",
    "ordered", "ordering", "packet", "packets", "performance", "receiver",
    "receivers", "reliability", "reliable", "sender", "senders", "sequence",
    "service", "services", "stream", "streams", "streaming", "timing",
    "traffic", "transport", "transmission", "video",
}


def _sanitize_source_text_for_questions(source_text: str) -> str:
    """
    Remove administrative and non-learning content before sending text to the LLM.
    """
    if not source_text:
        return ""

    blocks = re.split(r"\n\s*\n", source_text)
    kept = []

    for block in blocks:
        low = block.lower()

        if any(marker in low for marker in ADMIN_MARKERS):
            continue

        # strip emails / urls / obvious instructor roster lines
        lines = []
        for line in block.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()

            if not stripped:
                continue
            if "@" in stripped:
                continue
            if lowered.startswith("http://") or lowered.startswith("https://"):
                continue
            if lowered.startswith("dr."):
                continue
            if re.match(r"^(Dr\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$", stripped):
                continue

            lines.append(stripped)

        cleaned = "\n".join(lines).strip()
        if cleaned:
            kept.append(cleaned)

    return "\n\n".join(kept).strip()


def _topic_terms(topic: str) -> list[str]:
    normalized = re.sub(r"[^a-z0-9\s/-]", " ", (topic or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    terms = []

    for token in re.split(r"[\s/-]+", normalized):
        if len(token) < 3 or token in TOPIC_STOPWORDS:
            continue
        terms.append(token)
        if token.endswith("s") and len(token) > 4:
            terms.append(token[:-1])

    seen = set()
    deduped = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            deduped.append(term)
    return deduped


def _split_large_paragraph(paragraph: str, max_chunk_size: int) -> list[str]:
    paragraph = paragraph.strip()
    if not paragraph:
        return []
    if len(paragraph) <= max_chunk_size:
        return [paragraph]

    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    parts = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        candidate = sentence if not current else f"{current} {sentence}"
        if len(candidate) <= max_chunk_size:
            current = candidate
            continue

        if current:
            parts.append(current)
            current = ""

        if len(sentence) <= max_chunk_size:
            current = sentence
            continue

        start = 0
        while start < len(sentence):
            end = min(start + max_chunk_size, len(sentence))
            if end < len(sentence):
                split_at = sentence.rfind(" ", start, end)
                if split_at > start + int(max_chunk_size * 0.6):
                    end = split_at
            parts.append(sentence[start:end].strip())
            start = end
            while start < len(sentence) and sentence[start].isspace():
                start += 1

    if current:
        parts.append(current)

    return [part for part in parts if part]


def _split_source_into_chunks(source_text: str, target_chunk_size: int = TARGET_CHUNK_SIZE) -> list[str]:
    paragraphs = re.split(r"\n\s*\n", source_text or "")
    units = []

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.extend(_split_large_paragraph(paragraph, MAX_CHUNK_SIZE))

    chunks = []
    current = ""

    for unit in units:
        candidate = unit if not current else f"{current}\n\n{unit}"
        if len(candidate) <= MAX_CHUNK_SIZE or len(current) < int(target_chunk_size * 0.7):
            current = candidate
            continue

        if current:
            chunks.append(current)
        current = unit

    if current:
        chunks.append(current)

    return chunks


def _score_chunk_for_topic(chunk: str, topic: str) -> float:
    chunk_lower = chunk.lower()
    topic_phrase = re.sub(r"\s+", " ", (topic or "").strip().lower())
    terms = _topic_terms(topic)

    score = 0.0
    matched_terms = 0

    if topic_phrase and topic_phrase in chunk_lower:
        score += 8.0

    for term in terms:
        matches = re.findall(rf"\b{re.escape(term)}\b", chunk_lower)
        if not matches:
            continue
        matched_terms += 1
        score += 2.0
        score += min(len(matches), 3) * 0.5

    if terms and matched_terms == len(terms):
        score += 3.0
    elif matched_terms >= 2:
        score += 1.5

    if matched_terms and any(marker in chunk_lower for marker in DEFINITION_MARKERS):
        score += 1.0

    if matched_terms and any(marker in chunk_lower for marker in EXPLANATION_MARKERS):
        score += 0.5

    return score


def _balanced_chunk_indices(chunk_count: int) -> list[int]:
    if chunk_count <= 0:
        return []
    if chunk_count <= 3:
        return list(range(chunk_count))

    indices = [0, chunk_count // 2, chunk_count - 1]
    if chunk_count >= 6:
        indices.insert(1, chunk_count // 3)

    seen = set()
    ordered = []
    for idx in indices:
        bounded = max(0, min(idx, chunk_count - 1))
        if bounded not in seen:
            seen.add(bounded)
            ordered.append(bounded)
    return ordered


def _selected_context_length(chunks: list[str], indices: set[int]) -> int:
    return sum(len(chunks[idx]) for idx in indices)


def _expand_chunk_selection(
    chunks: list[str],
    selected_indices: list[int],
    scores: list[float],
    min_chars: int,
    max_chars: int,
) -> list[int]:
    selected = set(selected_indices)
    if not selected:
        return []

    target_chars = min(max_chars, max(min_chars, _selected_context_length(chunks, selected)))

    while len(selected) < len(chunks):
        current_chars = _selected_context_length(chunks, selected)
        if current_chars >= target_chars:
            break

        candidates = set()
        for idx in selected:
            if idx - 1 >= 0 and idx - 1 not in selected:
                candidates.add(idx - 1)
            if idx + 1 < len(chunks) and idx + 1 not in selected:
                candidates.add(idx + 1)

        if not candidates:
            candidates = {idx for idx in _balanced_chunk_indices(len(chunks)) if idx not in selected}
        if not candidates:
            break

        best_idx = min(
            candidates,
            key=lambda idx: (
                -scores[idx],
                min(abs(idx - chosen) for chosen in selected),
                idx,
            ),
        )

        next_chars = current_chars + len(chunks[best_idx])
        if next_chars > max_chars and current_chars >= min_chars:
            break

        selected.add(best_idx)

    return sorted(selected)


def _select_question_context(source_text: str, topic: str) -> tuple[str, str, int]:
    if len(source_text) <= MAX_DIRECT_SOURCE_CHARS:
        return source_text, "full_cleaned_text", 1

    chunks = _split_source_into_chunks(source_text)
    if len(chunks) <= 1:
        return source_text, "full_cleaned_text", len(chunks) or 1

    scores = [_score_chunk_for_topic(chunk, topic) for chunk in chunks]
    ranked_indices = sorted(range(len(chunks)), key=lambda idx: (-scores[idx], idx))
    positive_scores = [score for score in scores if score > 0]
    max_score = max(scores) if scores else 0.0
    weak_scoring = not positive_scores or max_score < 2.5

    if weak_scoring:
        seed_indices = _balanced_chunk_indices(len(chunks))
        mode = "balanced_fallback"
    else:
        desired_count = min(MAX_TOPIC_CHUNKS, max(2, len(chunks) // 3))
        seed_indices = ranked_indices[:desired_count]
        mode = "topic_chunks"

    selected_indices = _expand_chunk_selection(
        chunks=chunks,
        selected_indices=seed_indices,
        scores=scores,
        min_chars=MIN_CONTEXT_CHARS,
        max_chars=MAX_CONTEXT_CHARS,
    )

    context = "\n\n".join(chunks[idx] for idx in selected_indices)
    return context, mode, len(seed_indices)


def _choose_variation_angle(difficulty: int) -> tuple[str, str]:
    if difficulty <= 2:
        return random.choice(VERY_EASY_EASY_ANGLES), "very_easy_easy"
    if difficulty == 3:
        return random.choice(MEDIUM_ANGLES), "medium"
    if difficulty == 4:
        return random.choice(HARD_ANGLES), "hard"
    return random.choice(VERY_HARD_ANGLES), "very_hard"


def _significant_terms(text: str) -> set[str]:
    terms = set()
    normalized = re.sub(r"[^a-z0-9\s-]", " ", (text or "").lower())
    for token in re.split(r"[\s-]+", normalized):
        if len(token) < 4 or token in TOPIC_STOPWORDS:
            continue
        terms.add(token)
        if token.endswith("s") and len(token) > 5:
            terms.add(token[:-1])
    return terms


def _has_high_difficulty_reasoning_layer(question_text: str) -> bool:
    text = question_text.strip().lower()
    if not text:
        return False

    return any(marker in text for marker in HIGH_DIFFICULTY_REASONING_MARKERS)


def _extract_numeric_tokens(text: str) -> set[str]:
    matches = re.findall(
        r"\b\d+(?:\.\d+)?(?:\s?(?:%|percent|ms|millisecond(?:s)?|second(?:s)?|sec|minute(?:s)?|hour(?:s)?|kbps|mbps|gbps|hz|khz|mhz|ghz|fps|bytes?|packets?))?\b",
        (text or "").lower(),
    )
    return {re.sub(r"\s+", "", match.strip()) for match in matches if match.strip()}


def _source_terms(source_text: str) -> set[str]:
    return _significant_terms(source_text)


def _expand_term_variants(terms: set[str]) -> set[str]:
    expanded = set()
    for term in terms:
        if not term:
            continue
        expanded.add(term)
        if term.endswith("s") and len(term) > 5:
            expanded.add(term[:-1])
        elif len(term) > 4:
            expanded.add(f"{term}s")
    return expanded


def _allowed_domain_terms_for_question(topic: str, source_text: str) -> set[str]:
    base_terms = _source_terms(source_text) | set(_topic_terms(topic))
    return (
        _expand_term_variants(base_terms)
        | DOMAIN_GENERAL_ALLOWED_TERMS
        | GENERIC_QUESTION_TERMS
    )


def _question_text_blob(q: dict) -> str:
    options = " ".join(str(v) for v in (q.get("options", {}) or {}).values())
    return " ".join([
        str(q.get("question", "")),
        options,
    ]).strip()


def _unsupported_specifics_breakdown(q: dict, source_text: str) -> dict:
    allowed_terms = _allowed_domain_terms_for_question(str(q.get("topic", "")), source_text)
    stem_terms = _significant_terms(str(q.get("question", "")))
    stem_unsupported = stem_terms - allowed_terms

    option_unsupported_terms = []
    options_with_any_unsupported = 0
    option_specific_count = 0

    for option_text in (q.get("options", {}) or {}).values():
        unsupported = _significant_terms(str(option_text)) - allowed_terms
        option_unsupported_terms.append(unsupported)
        if unsupported:
            options_with_any_unsupported += 1
        if len(unsupported) >= 2:
            option_specific_count += 1

    distinct_terms = set(stem_unsupported)
    for terms in option_unsupported_terms:
        distinct_terms.update(terms)

    total_mentions = len(stem_unsupported) + sum(len(terms) for terms in option_unsupported_terms)

    return {
        "stem_terms": stem_unsupported,
        "option_terms": option_unsupported_terms,
        "distinct_terms": distinct_terms,
        "options_with_any_unsupported": options_with_any_unsupported,
        "option_specific_count": option_specific_count,
        "total_mentions": total_mentions,
    }


def _significant_ngrams(text: str, n: int = 3) -> set[str]:
    tokens = [
        token for token in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(token) >= 4 and token not in TOPIC_STOPWORDS and token not in GENERIC_QUESTION_TERMS
    ]
    if len(tokens) < n:
        return set()
    return {
        " ".join(tokens[idx:idx + n])
        for idx in range(len(tokens) - n + 1)
    }


def _introduces_unsupported_numeric_detail(q: dict, source_text: str) -> bool:
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4 or not source_text:
        return False

    question_numbers = _extract_numeric_tokens(_question_text_blob(q))
    if not question_numbers:
        return False

    source_numbers = _extract_numeric_tokens(source_text)
    return any(token not in source_numbers for token in question_numbers)


def _introduces_too_many_out_of_source_specifics(q: dict, source_text: str) -> bool:
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4 or not source_text:
        return False

    breakdown = _unsupported_specifics_breakdown(q, source_text)
    stem_term_count = len(breakdown["stem_terms"])
    distinct_count = len(breakdown["distinct_terms"])
    option_specific_count = breakdown["option_specific_count"]
    options_with_any_unsupported = breakdown["options_with_any_unsupported"]
    total_mentions = breakdown["total_mentions"]

    should_reject = (
        distinct_count >= 8
        and option_specific_count >= 2
        and (stem_term_count >= 2 or total_mentions >= 10)
    ) or (
        distinct_count >= 10
        and options_with_any_unsupported >= 3
        and total_mentions >= 12
    )

    if should_reject:
        logger.info(
            "[LLM] Out-of-source specifics topic=%s stem_terms=%s option_count=%s",
            q.get("topic"),
            sorted(breakdown["stem_terms"])[:5],
            option_specific_count,
        )

    return should_reject


def _looks_like_direct_suitability_contrast(question_text: str) -> bool:
    text = question_text.strip().lower()
    if not text:
        return False

    comparison_markers = [
        "more suitable",
        "less suitable",
        "better suited",
        "more appropriate",
        "preferred over",
        "rather than",
        "instead of",
        "over ",
    ]
    premise_markers = [
        "which protocol",
        "which option",
        "why is",
        "what protocol characteristic",
        "when timely delivery",
        "when ordering matters",
    ]

    return (
        any(marker in text for marker in comparison_markers) and
        any(marker in text for marker in premise_markers)
    )


def _looks_like_direct_restatement_question(question_text: str) -> bool:
    text = question_text.strip().lower()
    if not text:
        return False

    if _has_high_difficulty_reasoning_layer(text):
        return False

    if _looks_like_direct_suitability_contrast(text):
        return True

    return any(
        text.startswith(prefix) or prefix in text
        for prefix in DIRECT_RESTATEMENT_PREFIXES
    )


def _high_difficulty_looks_too_direct(q: dict) -> bool:
    question_text = str(q.get("question", "")).strip()
    if not question_text:
        return False

    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4:
        return False

    if not _looks_like_direct_restatement_question(question_text):
        return False

    return True


def _high_difficulty_is_too_close_to_source(q: dict, source_text: str) -> bool:
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4 or not source_text:
        return False

    question_text = str(q.get("question", "")).strip()
    if not question_text:
        return False

    question_ngrams = _significant_ngrams(question_text, n=3)
    if not question_ngrams:
        return False

    source_ngrams = _significant_ngrams(source_text, n=3)
    overlap = question_ngrams & source_ngrams
    if not overlap:
        return False

    if _has_high_difficulty_reasoning_layer(question_text):
        return False

    if not _looks_like_direct_restatement_question(question_text):
        return False

    if difficulty >= 5:
        return len(overlap) >= 2

    return len(overlap) >= 4


def _high_difficulty_distractors_look_too_weak(q: dict) -> bool:
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4:
        return False

    options = q.get("options", {}) or {}
    correct_letter = q.get("correct_answer")
    correct_text = str(options.get(correct_letter, "")).strip()
    distractors = [
        str(text).strip()
        for letter, text in options.items()
        if letter != correct_letter and str(text).strip()
    ]

    if len(distractors) != 3 or not correct_text:
        return False

    correct_words = len(correct_text.split())
    correct_len = len(correct_text)
    distractor_lengths = [len(text) for text in distractors]
    short_distractors = sum(
        1 for text in distractors
        if len(text.split()) <= 3 or len(text) < max(12, int(correct_len * 0.45))
    )

    if correct_words >= 7 and short_distractors >= 2:
        return True

    if correct_len >= 30 and sum(distractor_lengths) / len(distractor_lengths) < correct_len * 0.6:
        return True

    anchor_terms = (
        set(_topic_terms(str(q.get("topic", ""))))
        | _significant_terms(str(q.get("question", "")))
        | _significant_terms(correct_text)
    )

    weak_overlap = 0
    for distractor in distractors:
        lowered = distractor.lower()
        if any(marker in lowered for marker in WEAK_DISTRACTOR_MARKERS):
            return True

        overlap = anchor_terms & _significant_terms(distractor)
        if not overlap and len(distractor.split()) <= 5:
            weak_overlap += 1

    return weak_overlap >= 2


def _looks_like_admin_question(q: dict) -> bool:
    combined = " ".join([
        str(q.get("question", "")),
        str(q.get("explanation", "")),
        " ".join(str(v) for v in q.get("options", {}).values()),
    ]).lower()

    return any(marker in combined for marker in FORBIDDEN_QUESTION_MARKERS)


def _looks_like_plain_recall_question(question_text: str) -> bool:
    text = question_text.strip().lower()
    if not text:
        return False

    if any(marker in text for marker in REASONING_MARKERS):
        return False

    return any(text.startswith(marker) or marker in text for marker in PLAIN_RECALL_MARKERS)


def _looks_like_reasoning_question(question_text: str) -> bool:
    text = question_text.strip().lower()
    if not text:
        return False

    return any(marker in text for marker in REASONING_MARKERS)


def _difficulty_mismatch_reasons(q: dict, source_text: str | None = None) -> list[str]:
    question_text = str(q.get("question", ""))
    reasons = []

    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty >= 4 and _looks_like_plain_recall_question(question_text):
        reasons.append("plain_recall_high_difficulty")

    if difficulty >= 4 and _high_difficulty_looks_too_direct(q):
        reasons.append("too_direct_high_difficulty")

    if difficulty >= 4 and _high_difficulty_distractors_look_too_weak(q):
        reasons.append("weak_distractors_high_difficulty")

    if source_text and difficulty >= 4 and _introduces_unsupported_numeric_detail(q, source_text):
        reasons.append("unsupported_numeric_detail")

    if source_text and difficulty >= 4 and _introduces_too_many_out_of_source_specifics(q, source_text):
        reasons.append("too_many_out_of_source_specifics")

    if source_text and difficulty >= 4 and _high_difficulty_is_too_close_to_source(q, source_text):
        reasons.append("too_close_to_source")

    if difficulty <= 2 and _looks_like_reasoning_question(question_text):
        reasons.append("too_reasoning_heavy_low_difficulty")

    return reasons


async def _generate_question_once(topic: str, difficulty: int, source_text: str) -> dict:
    difficulty_label = {
    1: "very easy",
    2: "easy",
    3: "medium",
    4: "hard",
    5: "very hard",
    }[difficulty]
    variation, variation_bucket = _choose_variation_angle(difficulty)

    clean_source_text = _sanitize_source_text_for_questions(source_text) or source_text
    selected_context, context_mode, selected_chunk_count = _select_question_context(
        clean_source_text,
        topic,
    )

    logger.info(
        "[LLM] Question context selected mode=%s original_chars=%s selected_chars=%s topic=%s seed_chunks=%s",
        context_mode,
        len(clean_source_text),
        len(selected_context),
        topic,
        selected_chunk_count,
    )
    logger.info(
        "[LLM] Question difficulty calibration difficulty=%s variation_bucket=%s topic=%s",
        difficulty,
        variation_bucket,
        topic,
    )

    prompt = PROMPT_TEMPLATE.format(
        variation=variation,
        topic=topic,
        difficulty_label=difficulty_label,
        difficulty=difficulty,
        source_text=selected_context
    )

    all_failures = []
    reserve_candidate = None

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logger.warning(f"[LLM] Unknown provider in PROVIDER_ORDER: {provider_name}")
                continue

            cooldown_remaining = _provider_cooldown_remaining(provider_name)
            if cooldown_remaining > 0:
                logger.info(
                    "[LLM] Skipping provider=%s cooldown_remaining=%ss",
                    provider_name,
                    int(cooldown_remaining),
                )
                continue

            if not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} (missing API key)")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            if not models:
                logger.info(f"[LLM] Skipping provider={provider_name} (no models configured)")
                continue

            logger.info(f"[LLM] Trying provider={provider_name} models={models}")

            for model in models:
                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw)

                    q = json.loads(raw)

                    is_valid, reasons = validate_question(q, selected_context)
                    if is_valid:
                        q = _shuffle_question_options(q)
                        logger.info(f"[LLM] SUCCESS provider={provider_name} model={model} topic={topic} difficulty={difficulty}")
                        return q
                    else:
                        reason_text = ",".join(reasons) if reasons else "unknown"
                        msg = f"[LLM] Validation failed provider={provider_name} model={model} reasons={reason_text}"
                        logger.warning(msg)
                        all_failures.append(msg)

                        core_valid, _ = validate_question_core_only(q)
                        if core_valid and reserve_candidate is None:
                            reserve_candidate = {
                                "question": q,
                                "provider": provider_name,
                                "model": model,
                                "strict_reasons": reasons,
                            }
                            logger.info(
                                "[LLM] Reserve candidate stored provider=%s model=%s reasons=%s",
                                provider_name,
                                model,
                                reason_text,
                            )
                        continue

                except ProviderHTTPError as e:
                    _record_provider_throttle(e.provider, e.status_code)
                    msg = f"[LLM] HTTP failure provider={e.provider} model={e.model} status={e.status_code} details={e.message}"
                    logger.warning(msg)
                    all_failures.append(msg)

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. "
                            f"Check payload/model compatibility. Details: {e.message}"
                        )

                    # fallback to next model/provider
                    continue

                except json.JSONDecodeError as e:
                    msg = f"[LLM] JSON parse failed provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

                except Exception as e:
                    msg = f"[LLM] Unexpected error provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

    if reserve_candidate is not None:
        question = _shuffle_question_options(reserve_candidate["question"])
        logger.warning(
            "[LLM] LAST_RESORT_ACCEPT provider=%s model=%s strict_reasons=%s",
            reserve_candidate["provider"],
            reserve_candidate["model"],
            ",".join(reserve_candidate["strict_reasons"]) if reserve_candidate["strict_reasons"] else "unknown",
        )
        return question

    raise NoValidQuestionError("All providers/models failed to produce a valid question.\n" + "\n".join(all_failures))


async def generate_question(topic: str, difficulty: int, source_text: str) -> dict:
    try:
        return await _generate_question_once(topic, difficulty, source_text)
    except NoValidQuestionError as primary_error:
        fallback_difficulty = None
        if difficulty == 5:
            fallback_difficulty = 4
        elif difficulty == 4:
            fallback_difficulty = 3

        if fallback_difficulty is None:
            raise ValueError(str(primary_error)) from primary_error

        logger.warning(
            "[LLM] Falling back generation topic=%s requested_difficulty=%s fallback_difficulty=%s",
            topic,
            difficulty,
            fallback_difficulty,
        )

        try:
            return await _generate_question_once(topic, fallback_difficulty, source_text)
        except NoValidQuestionError as fallback_error:
            raise ValueError(str(fallback_error)) from primary_error

# Rewrite the explanation in much simpler language.
# Use an analogy if it helps.
# Be concise — 2 to 3 sentences maximum.
# Do not introduce new topics.

async def generate_simple_explanation(
    topic: str,
    question: str,
    explanation: str,
) -> str:
    prompt = f"""
A student got this question wrong and needs a simpler explanation.

Topic: {topic}
Question: {question}
Original explanation: {explanation}

Rewrite the explanation in much simpler language for a beginner student.
- Keep it factually consistent with the original explanation.
- Define any technical term briefly if needed.
- Use an analogy only if it genuinely clarifies the concept.
- Be concise — 2 to 3 sentences maximum.
- Do not introduce new topics or extra facts not already implied by the original explanation.

Respond with ONLY the simpler explanation text, no preamble.
"""

    all_failures = []

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg or not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} for simpler explanation")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw).strip()

                    if raw and len(raw) >= 10:
                        logger.info(f"[LLM] SUCCESS simpler_explanation provider={provider_name} model={model}")
                        return raw

                    msg = f"[LLM] Empty/short simpler explanation provider={provider_name} model={model}"
                    logger.warning(msg)
                    all_failures.append(msg)

                except ProviderHTTPError as e:
                    msg = f"[LLM] HTTP failure simpler_explanation provider={e.provider} model={e.model} status={e.status_code} details={e.message}"
                    logger.warning(msg)
                    all_failures.append(msg)

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. Details: {e.message}"
                        )

                    continue

                except Exception as e:
                    msg = f"[LLM] Unexpected simpler_explanation error provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

    raise ValueError("All providers/models failed to produce a simpler explanation.\n" + "\n".join(all_failures))

async def extract_content_metadata(sample_text: str) -> dict:
    """
    Use the LLM fallback chain to extract metadata only from lecture text.
    Returns:
      suggested_title, suggested_week, topics, summary
    """
    prompt = CONTENT_EXTRACTION_PROMPT.format(text=sample_text)

    all_failures = []

    async with httpx.AsyncClient(timeout=60) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg or not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} for content metadata")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw).strip()

                    result = json.loads(raw)

                    required = ["suggested_title", "suggested_week", "topics", "summary"]
                    if not all(k in result for k in required):
                        msg = f"[LLM] Metadata extraction missing fields provider={provider_name} model={model}"
                        logger.warning(msg)
                        all_failures.append(msg)
                        continue

                    if not isinstance(result["topics"], list) or len(result["topics"]) == 0:
                        msg = f"[LLM] Metadata extraction returned empty topics provider={provider_name} model={model}"
                        logger.warning(msg)
                        all_failures.append(msg)
                        continue

                    try:
                        result["suggested_week"] = int(result["suggested_week"])
                    except (TypeError, ValueError):
                        result["suggested_week"] = 1

                    result["topics"] = [str(t).strip() for t in result["topics"] if str(t).strip()]
                    result["topics"] = result["topics"][:6]

                    # Safety: discard anything extra the model may hallucinate
                    result.pop("source_text", None)
                    result.pop("suggested_content_type", None)

                    logger.info(
                        f"[LLM] SUCCESS metadata_extraction provider={provider_name} "
                        f"model={model} topics={len(result['topics'])}"
                    )
                    return result

                except json.JSONDecodeError as e:
                    msg = f"[LLM] JSON parse failed metadata_extraction provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

                except ProviderHTTPError as e:
                    msg = f"[LLM] HTTP failure metadata_extraction provider={e.provider} model={e.model} status={e.status_code} details={e.message}"
                    logger.warning(msg)
                    all_failures.append(msg)

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. Details: {e.message}"
                        )
                    continue

                except Exception as e:
                    msg = f"[LLM] Unexpected metadata_extraction error provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

    raise ValueError(
        "All providers/models failed to extract content metadata.\n" + "\n".join(all_failures)
    )
