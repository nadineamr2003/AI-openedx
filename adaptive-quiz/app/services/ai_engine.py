import httpx
import os
import json
import logging
from dotenv import load_dotenv
import random
from typing import Any, Dict, List, Optional

load_dotenv()

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
- Difficulty: {difficulty_label} (easy=recall, medium=application, hard=analysis)
- Exactly 4 answer choices labeled A, B, C, D
- Exactly 1 correct answer
- A short explanation (2-3 sentences) for why the correct answer is right
- Stay grounded in the provided material — do NOT invent facts
- All distractors must be plausible, not obviously wrong

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
    return True


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
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message", json.dumps(err))
        if isinstance(err, str):
            return err
    return "Unknown error"


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

    resp = await client.post(endpoint, headers=headers, json=payload)
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code >= 400:
        raise ProviderHTTPError(
            provider=provider_name,
            model=model,
            status_code=resp.status_code,
            message=_extract_error_message(data),
        )

    return _extract_message_content(data)


async def generate_question(topic: str, difficulty: int, source_text: str) -> dict:
    difficulty_label = {1: "easy", 2: "medium", 3: "hard"}[difficulty]
    variation = random.choice(VARIATION_ANGLES)

    prompt = PROMPT_TEMPLATE.format(
        variation=variation,
        topic=topic,
        difficulty_label=difficulty_label,
        difficulty=difficulty,
        source_text=source_text[:3000]
    )

    all_failures = []

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logging.warning(f"Unknown provider in PROVIDER_ORDER: {provider_name}")
                continue

            if not provider_cfg["api_key"]:
                logging.info(f"Skipping provider {provider_name}: missing API key")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            if not models:
                logging.info(f"Skipping provider {provider_name}: no models configured")
                continue

            logging.info(f"Trying provider: {provider_name}")

            for model in models:
                try:
                    logging.info(f"Trying {provider_name} / {model}")
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw)

                    q = json.loads(raw)

                    if validate_question(q):
                        logging.info(f"✅ Question generated by {provider_name} / {model}")
                        return q
                    else:
                        msg = f"Validation failed for {provider_name} / {model}"
                        logging.warning(msg)
                        all_failures.append(msg)
                        continue

                except ProviderHTTPError as e:
                    msg = f"{e.provider}/{e.model} -> HTTP {e.status_code}: {e.message}"
                    logging.warning(msg)
                    all_failures.append(msg)

                    # 400 usually means our request shape is wrong. Stop immediately.
                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. "
                            f"Check payload/model compatibility. Details: {e.message}"
                        )

                    # Invalid key / forbidden / model not found -> move within or across providers
                    if e.status_code in {401, 403, 404}:
                        continue

                    # Quota / rate limit / timeout / server errors -> try next model/provider
                    if _is_retryable_status(e.status_code):
                        continue

                    continue

                except json.JSONDecodeError:
                    msg = f"JSON parse failed for {provider_name} / {model}"
                    logging.warning(msg)
                    all_failures.append(msg)
                    continue

                except Exception as e:
                    msg = f"Unexpected error with {provider_name} / {model}: {e}"
                    logging.error(msg)
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
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw).strip()

                    if raw and len(raw) >= 10:
                        logging.info(f"✅ Simple explanation generated by {provider_name} / {model}")
                        return raw

                    all_failures.append(f"Empty/short text from {provider_name} / {model}")

                except ProviderHTTPError as e:
                    msg = f"{e.provider}/{e.model} -> HTTP {e.status_code}: {e.message}"
                    logging.warning(msg)
                    all_failures.append(msg)

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. Details: {e.message}"
                        )

                    continue

                except Exception as e:
                    msg = f"Unexpected error with {provider_name} / {model}: {e}"
                    logging.error(msg)
                    all_failures.append(msg)
                    continue

    raise ValueError("All providers/models failed to produce a simpler explanation.\n" + "\n".join(all_failures))