import httpx
import os
import json
import logging
import re
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
    ["gemini", "groq", "openrouter", "huggingface"]
)

# Keep provider model lists in env so you can tune them without editing code.
PROVIDER_MODELS = {
    "gemini": _csv_env("GEMINI_MODELS", ["gemini-3-flash-preview"]),
    "groq": _csv_env("GROQ_MODELS", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]),
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

VARIATION_ANGLES = [
    "Focus on a practical use case or real example.",
    "Focus on common mistakes or misconceptions.",
    "Focus on the definition or core concept.",
    "Focus on comparing this concept to a related one.",
    "Focus on what happens when this concept is applied incorrectly.",
    "Focus on the steps or process involved.",
]

PROMPT_TEMPLATE = """
Generate one multiple-choice question based ONLY on the provided text.
Requirements:
- Be creative with the question angle — avoid repeating common phrasings
- Question angle: {variation}
- Topic: {topic}
- Difficulty: {difficulty_label} (very easy = very basic recognition / obvious recall, easy = straightforward recall, medium = normal application, hard = multi-step reasoning or comparison, very hard = deeper analysis, nuanced distinction, or trickier application)
- Exactly 4 answer choices labeled A, B, C, D
- Exactly 1 correct answer
- Randomize which answer choice is correct. It must not systematically be A.
- A short explanation (2-3 sentences) for why the correct answer is right
- Do not mention answer letters in the explanation. Explain using the concept/content itself.
- Stay grounded in the provided material — do NOT invent facts
- All distractors must be plausible, not obviously wrong
- Ignore instructor names, staff names, office hours, course logistics, grades, contact details, URLs, and administrative information
- Never ask about who teaches the course, staff members, email addresses, office hours, access codes, or grade distribution

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


def validate_question(q: dict) -> bool:
    required = ["question", "options", "correct_answer", "explanation", "topic", "difficulty"]
    if not all(k in q for k in required):
        return False
    if not isinstance(q["options"], dict):
        return False
    if q["correct_answer"] not in q["options"]:
        return False
    if len(q["options"]) != 4:
        return False
    if len(q["question"].strip()) < 10:
        return False
    if _looks_like_admin_question(q):
        return False
    return True

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


def _looks_like_admin_question(q: dict) -> bool:
    combined = " ".join([
        str(q.get("question", "")),
        str(q.get("explanation", "")),
        " ".join(str(v) for v in q.get("options", {}).values()),
    ]).lower()

    return any(marker in combined for marker in FORBIDDEN_QUESTION_MARKERS)


async def generate_question(topic: str, difficulty: int, source_text: str) -> dict:
    difficulty_label = {
    1: "very easy",
    2: "easy",
    3: "medium",
    4: "hard",
    5: "very hard",
    }[difficulty]
    variation = random.choice(VARIATION_ANGLES)

    clean_source_text = _sanitize_source_text_for_questions(source_text) or source_text

    prompt = PROMPT_TEMPLATE.format(
        variation=variation,
        topic=topic,
        difficulty_label=difficulty_label,
        difficulty=difficulty,
        source_text=clean_source_text[:4000]
    )

    all_failures = []

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logger.warning(f"[LLM] Unknown provider in PROVIDER_ORDER: {provider_name}")
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

                    if validate_question(q):
                        q = _shuffle_question_options(q)
                        logger.info(f"[LLM] SUCCESS provider={provider_name} model={model} topic={topic} difficulty={difficulty}")
                        return q
                    else:
                        msg = f"[LLM] Validation failed provider={provider_name} model={model}"
                        logger.warning(msg)
                        all_failures.append(msg)
                        continue

                except ProviderHTTPError as e:
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

    raise ValueError("All providers/models failed to produce a valid question.\n" + "\n".join(all_failures))

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

Rewrite the explanation in much simpler language.
Use an analogy if it helps.
Be concise — 2 to 3 sentences maximum.
Do not introduce new topics.

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