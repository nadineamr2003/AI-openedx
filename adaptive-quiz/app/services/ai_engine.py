import asyncio
import httpx
import os
import json
import logging
import math
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
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
FIREWORKS_API_BASE = os.getenv("FIREWORKS_API_BASE", "https://api.fireworks.ai/inference/v1").strip()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_API_BASE = os.getenv("MISTRAL_API_BASE", "https://api.mistral.ai/v1").strip()

# =========================
# HELPERS
# =========================
def _csv_env(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


def _append_configured_provider_fallbacks(order: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for provider_name in order:
        if provider_name in seen:
            continue
        seen.add(provider_name)
        normalized.append(provider_name)

    for provider_name, api_key in (
        ("fireworks", FIREWORKS_API_KEY),
        ("mistral", MISTRAL_API_KEY),
    ):
        if api_key and provider_name not in seen:
            normalized.append(provider_name)
            seen.add(provider_name)

    return normalized

PROVIDER_ORDER = _csv_env(
    "PROVIDER_ORDER",
    ["gemini", "groq", "cerebras", "fireworks", "mistral", "openrouter", "huggingface"]
)
PROVIDER_ORDER = _append_configured_provider_fallbacks(PROVIDER_ORDER)

DIAGNOSTIC_PROVIDER_PRIORITY = ["cerebras", "huggingface", "groq"]
GIT_CHALLENGE_SAFE_PROVIDER_ORDER = [
    "groq",
    "cerebras",
    "huggingface",
    "fireworks",
    "mistral",
]
GIT_CHALLENGE_SAFE_MODEL_PREFERENCES = {
    "groq": ["llama-3.1-8b-instant"],
    "cerebras": ["llama-3.1-8b", "qwen-3-235b-a22b-instruct-2507"],
    "huggingface": ["meta-llama/Llama-3.1-8B-Instruct:cerebras"],
    "fireworks": ["accounts/fireworks/models/deepseek-v3p1"],
    "mistral": ["mistral-small-latest"],
}

# Keep provider model lists in env so you can tune them without editing code.
PROVIDER_MODELS = {
    "gemini": _csv_env("GEMINI_MODELS", ["gemini-3-flash-preview"]),
    "groq": _csv_env("GROQ_MODELS", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]),
    "cerebras": _csv_env(
        "CEREBRAS_MODELS",
        [
            "llama-3.1-8b",
            "qwen-3-235b-a22b-instruct-2507",
        ],
    ),
    "fireworks": _csv_env(
        "FIREWORKS_MODELS",
        [
            "accounts/fireworks/models/deepseek-v3p1",
            "accounts/fireworks/models/deepseek-v3p2",
        ],
    ),
    "mistral": _csv_env(
        "MISTRAL_MODELS",
        [
            "mistral-small-latest",
            "mistral-medium-latest",
            "mistral-large-latest",
        ],
    ),
    "openrouter": _csv_env(
        "OPENROUTER_MODELS",
        [
            "openrouter/free",
            "openai/gpt-oss-120b:free",
            "openai/gpt-oss-20b:free",
            "z-ai/glm-4.5-air:free",
            "google/gemma-4-26b-a4b-it:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemma-3-4b-it:free",
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
    "fireworks": {
        "endpoint": f"{FIREWORKS_API_BASE.rstrip('/')}/chat/completions",
        "api_key": FIREWORKS_API_KEY,
    },
    "mistral": {
        "endpoint": f"{MISTRAL_API_BASE.rstrip('/')}/chat/completions",
        "api_key": MISTRAL_API_KEY,
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


def _provider_registry_audit() -> None:
    ordered_registry = list(dict.fromkeys(list(PROVIDER_ORDER) + list(PROVIDERS.keys())))
    configured_from_env: list[str] = []
    active_providers: list[str] = []
    skipped_providers: list[str] = []

    for provider_name in ordered_registry:
        provider_cfg = PROVIDERS.get(provider_name)
        provider_models = list(PROVIDER_MODELS.get(provider_name, []))
        provider_in_order = provider_name in PROVIDER_ORDER
        provider_has_key = bool(provider_cfg and provider_cfg.get("api_key"))
        provider_has_endpoint = bool(provider_cfg and provider_cfg.get("endpoint"))

        if provider_has_key:
            configured_from_env.append(provider_name)

        if provider_cfg and provider_in_order and provider_has_key and provider_has_endpoint and provider_models:
            active_providers.append(provider_name)
            logger.info(
                "[LLM] Provider models loaded provider=%s models=%s",
                provider_name,
                provider_models,
            )
            continue

        reason_parts: list[str] = []
        if not provider_cfg:
            reason_parts.append("missing_registry_entry")
        else:
            if not provider_in_order:
                reason_parts.append("not_in_provider_order")
            if not provider_has_key:
                reason_parts.append("missing_api_key")
            if not provider_has_endpoint:
                reason_parts.append("missing_endpoint")
            if not provider_models:
                reason_parts.append("no_models_configured")
        skipped_providers.append(f"{provider_name}({'+'.join(reason_parts) or 'inactive'})")

    logger.info(
        "[LLM] Configured providers from env: %s",
        ", ".join(configured_from_env) if configured_from_env else "none",
    )
    logger.info(
        "[LLM] Active providers: %s",
        ", ".join(active_providers) if active_providers else "none",
    )
    if skipped_providers:
        logger.info("[LLM] Skipped providers: %s", ", ".join(skipped_providers))

    if FIREWORKS_API_KEY and "fireworks" not in active_providers:
        logger.warning(
            "[LLM] Provider key present but inactive provider=fireworks in_order=%s models=%s registered=%s",
            "fireworks" in PROVIDER_ORDER,
            bool(PROVIDER_MODELS.get("fireworks")),
            "fireworks" in PROVIDERS,
        )
    if MISTRAL_API_KEY and "mistral" not in active_providers:
        logger.warning(
            "[LLM] Provider key present but inactive provider=mistral in_order=%s models=%s registered=%s",
            "mistral" in PROVIDER_ORDER,
            bool(PROVIDER_MODELS.get("mistral")),
            "mistral" in PROVIDERS,
        )


_provider_registry_audit()
_PROVIDER_RUNTIME_AUDIT_LOGGED = False

DIAGNOSTIC_PROVIDER_MODEL_PREFERENCES = {
    "groq": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
}

OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "").strip()
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "").strip()
_PROVIDER_COOLDOWNS: dict[str, float] = {}
_MODEL_COOLDOWNS: dict[str, float] = {}
_PROVIDER_503_FAILURES: dict[str, list[float]] = {}
_GIT_CHALLENGE_MODEL_SUPPRESSIONS: dict[str, float] = {}
_MODEL_INSTABILITY_EVENTS: dict[str, list[float]] = {}

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
{course_style_block}
{hard_quality_block}

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
  "course_name": "...",
  "suggested_title": "...",
  "suggested_week": 1,
  "topics": ["topic1", "topic2", "topic3", "topic4"],
  "summary": "..."
}}

Rules:
- course_name:
  - extract the formal course name if it is clearly present in the lecture text or cover/header material
  - prefer the full course title over short codes when both appear
  - if the course name is not clear, return an empty string

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
    def __init__(self, message: str, *, fallback_context: dict[str, Any] | None = None):
        self.fallback_context = dict(fallback_context or {})
        super().__init__(message)


HARD_SHORT_SCOPE_MAX_CHARS = 4000
HARD_SHORT_SCOPE_MAX_SEED_CHUNKS = 1
HARD_SHORT_SCOPE_MAX_PROVIDER_MODEL_ATTEMPTS = 4
HARD_SHORT_SCOPE_VALIDATION_FAILURE_LIMIT = 2
HARD_SHORT_SCOPE_PROVIDER_FAILURE_LIMIT = 2
HIGH_DIFFICULTY_BRITTLE_REASONS = {
    "plain_recall_high_difficulty",
    "too_direct_high_difficulty",
    "weak_distractors_high_difficulty",
    "unsupported_numeric_detail",
    "too_many_out_of_source_specifics",
    "too_close_to_source",
    "generic_benefit_stem_high_difficulty",
    "ambiguous_multi_positive_options_high_difficulty",
    "slogan_recall_high_difficulty",
    "stem_option_mismatch_named_concept",
    "stem_option_mismatch_described_concept",
    "git_visibility_push_vs_pull_confusion",
    "git_local_update_wrong_action",
    "git_branch_isolation_wrong_action",
    "git_merge_conflict_logic_failure",
    "git_remote_ahead_sync_failure",
    "git_branching_stem_too_elaborate",
    "git_commit_quality_wrong_focus",
    "git_ci_cd_topic_drift",
    "git_topic_family_misalignment",
    "git_merge_conflict_wording_failure",
    "git_dual_correct_operational_options",
}

LAST_RESORT_BLOCKING_REASONS = {
    "plain_recall_high_difficulty",
    "too_direct_high_difficulty",
    "generic_benefit_stem_high_difficulty",
    "ambiguous_multi_positive_options_high_difficulty",
    "slogan_recall_high_difficulty",
    "stem_option_mismatch_named_concept",
    "stem_option_mismatch_described_concept",
    "git_visibility_push_vs_pull_confusion",
    "git_local_update_wrong_action",
    "git_branch_isolation_wrong_action",
    "git_merge_conflict_logic_failure",
    "git_remote_ahead_sync_failure",
    "git_branching_stem_too_elaborate",
    "git_commit_quality_wrong_focus",
    "git_ci_cd_topic_drift",
    "git_topic_family_misalignment",
    "git_merge_conflict_wording_failure",
    "git_dual_correct_operational_options",
}

GIT_FRAGILE_TOPIC_BUCKETS = {
    "merge_conflicts",
    "commit_quality",
    "distributed_vcs",
    "ci_cd_fundamentals",
}

GIT_FRAGILE_LAST_RESORT_REASONS = {
    "git_topic_family_misalignment",
    "git_commit_quality_wrong_focus",
    "git_ci_cd_topic_drift",
    "git_merge_conflict_logic_failure",
    "git_merge_conflict_wording_failure",
    "git_local_update_wrong_action",
    "git_visibility_push_vs_pull_confusion",
    "git_dual_correct_operational_options",
}

NAMED_CONCEPT_ALIASES = {
    "command": ["command pattern"],
    "observer": ["observer pattern"],
    "strategy": ["strategy pattern"],
    "factory_method": ["factory method", "factory pattern", "abstract factory"],
    "decorator": ["decorator pattern"],
    "adapter": ["adapter pattern"],
    "singleton": ["singleton pattern"],
    "dependency_inversion": [
        "dependency inversion principle",
        "dependency inversion",
        "dip",
    ],
    "open_closed": [
        "open closed principle",
        "open-closed principle",
        "ocp",
    ],
    "single_responsibility": [
        "single responsibility principle",
        "single responsibility",
        "srp",
    ],
    "interface_segregation": [
        "interface segregation principle",
        "interface segregation",
        "isp",
    ],
    "liskov_substitution": [
        "liskov substitution principle",
        "liskov substitution",
        "lsp",
    ],
    "abstraction": ["abstraction"],
    "information_hiding": ["information hiding", "encapsulation"],
    "coupling": ["coupling", "low coupling", "tight coupling"],
    "cohesion": ["cohesion", "high cohesion", "low cohesion"],
}

OPTION_ONLY_CONCEPT_ALIASES = {
    "command": ["command"],
    "observer": ["observer"],
    "strategy": ["strategy"],
    "factory_method": ["factory"],
    "decorator": ["decorator"],
    "adapter": ["adapter"],
    "singleton": ["singleton"],
}

DESCRIBED_CONCEPT_HINTS = {
    "command": {
        "threshold": 2,
        "groups": [
            ("request as an object", "request as object", "encapsulate a request"),
            ("remote control", "toolbar button", "menu item"),
            ("invoker", "receiver"),
            ("undo", "redo"),
        ],
    },
    "observer": {
        "threshold": 2,
        "groups": [
            ("notify", "notification"),
            ("observer", "observers", "subscriber", "subscribers", "listener", "listeners"),
            ("subject", "one-to-many", "state changes", "state change"),
        ],
    },
    "strategy": {
        "threshold": 2,
        "groups": [
            ("family of algorithms", "algorithm", "algorithms"),
            ("swap", "switch", "choose"),
            ("runtime", "at runtime", "without changing the client"),
        ],
    },
    "dependency_inversion": {
        "threshold": 2,
        "groups": [
            ("high-level module", "high level module"),
            ("abstraction", "interface"),
            ("concrete implementation", "low-level module", "low level module"),
        ],
    },
    "open_closed": {
        "threshold": 2,
        "groups": [
            ("open for extension", "closed for modification"),
            ("extend", "extension"),
            ("without modifying", "without changing existing"),
        ],
    },
}

CONCEPT_FOCUS_TOKEN_MAP = {
    "command": "command_pattern",
    "observer": "observer_pattern",
    "strategy": "strategy_pattern",
    "factory_method": "factory_method_pattern",
    "decorator": "decorator_pattern",
    "adapter": "adapter_pattern",
    "singleton": "singleton_pattern",
    "dependency_inversion": "dip",
    "open_closed": "ocp",
    "single_responsibility": "srp",
    "interface_segregation": "isp",
    "liskov_substitution": "lsp",
    "abstraction": "abstraction",
    "information_hiding": "information_hiding",
    "coupling": "low_coupling",
    "cohesion": "high_cohesion",
}

CONCEPT_FOCUS_RULES = [
    (
        "adapter_wrapper_stack",
        [
            "linked list",
            "stack",
            "wrapper",
            "without modifying",
        ],
    ),
    (
        "wrapper_vs_modify_existing_class",
        [
            "wrapper",
            "modify the existing class",
            "without modifying",
            "existing class",
        ],
    ),
    (
        "clone_first_checkout",
        [
            "join the team",
            "first time",
            "local copy",
            "clone",
        ],
    ),
    (
        "pull_remote_updates",
        [
            "already has a local",
            "teammate",
            "pushed",
            "pull",
        ],
    ),
    (
        "commit_vs_push",
        [
            "committed locally",
            "teammates still",
            "push",
        ],
    ),
    (
        "local_vs_remote_repo",
        [
            "local repository",
            "remote repository",
        ],
    ),
    (
        "branch_isolation",
        [
            "isolated",
            "feature branch",
            "without affecting",
        ],
    ),
    (
        "branch_vs_trunk",
        [
            "branch",
            "trunk",
            "mainline",
        ],
    ),
    (
        "merge_commit_graph",
        [
            "two parent",
            "two previous commits",
            "merged histories",
        ],
    ),
    (
        "merge_conflict_resolution",
        [
            "merge conflict",
            "overlapping edits",
            "resolve",
        ],
    ),
    (
        "distributed_vcs",
        [
            "distributed version control",
            "full repository",
            "local history",
        ],
    ),
    (
        "commit_message_quality",
        [
            "commit message",
            "descriptive",
            "meaningful",
        ],
    ),
    (
        "ci_build_per_commit",
        [
            "continuous integration",
            "after commit",
            "build",
        ],
    ),
    (
        "delivery_vs_deployment",
        [
            "continuous delivery",
            "continuous deployment",
        ],
    ),
    (
        "early_user_involvement",
        [
            "involve users early",
            "real users early",
            "user feedback early",
        ],
    ),
    (
        "low_fidelity_iterative_design",
        [
            "low fidelity",
            "iterative",
            "paper prototype",
        ],
    ),
    (
        "aggregate_questions_single_dialog",
        [
            "single dialog",
            "single dialogue",
            "aggregate related questions",
            "aggregate many related questions",
            "many related questions into a single dialog",
        ],
    ),
    (
        "reversible_actions_vs_confirmation",
        [
            "reversible action",
            "reversible actions",
            "confirmation dialog",
            "confirmation before",
            "undo instead",
        ],
    ),
    (
        "horizontal_prototype",
        [
            "horizontal prototype",
        ],
    ),
    (
        "vertical_prototype",
        [
            "vertical prototype",
        ],
    ),
    ("t_prototype", ["t prototype", "t-shaped prototype", "t shaped prototype"]),
    ("local_prototype", ["local prototype"]),
    (
        "visible_feedback",
        [
            "visible feedback",
            "feedback after action",
            "acknowledgment after action",
        ],
    ),
    (
        "visible_state",
        [
            "visible system state",
            "visible state",
            "show current state",
        ],
    ),
    (
        "reduce_memory_load",
        [
            "recognition rather than recall",
            "memory load",
            "reduce memory load",
        ],
    ),
    (
        "accessibility_text_labels",
        [
            "text label",
            "icons alone",
            "screen reader",
            "accessible label",
        ],
    ),
    (
        "goal_of_testing",
        [
            "goal of testing",
            "find errors",
            "find defects",
        ],
    ),
    (
        "static_vs_dynamic_verification",
        [
            "dynamic verification",
            "static verification",
        ],
    ),
    ("static_verification", ["static verification"]),
    ("dynamic_verification", ["dynamic verification"]),
    ("inspections", ["inspection", "inspections", "formal review"]),
    ("static_analyzers", ["static analyzer", "static analyzers", "static analysis tool"]),
    (
        "programmer_vs_independent_tester",
        [
            "independent tester",
            "programmer tests",
            "separate tester",
        ],
    ),
    ("equivalence_partitioning", ["equivalence partition", "equivalence class"]),
    (
        "most_likely_to_find_errors",
        [
            "most likely to find",
            "find the most errors",
            "find more defects",
        ],
    ),
    (
        "white_box_code_knowledge",
        [
            "white box",
            "internal structure",
            "source code knowledge",
        ],
    ),
    (
        "test_suite_efficiency",
        [
            "test suite",
            "same coverage",
            "fewer test cases",
        ],
    ),
]


def _brittle_reason_subset(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if reason in HIGH_DIFFICULTY_BRITTLE_REASONS]


def _blocks_last_resort_accept(reasons: list[str]) -> bool:
    return any(reason in LAST_RESORT_BLOCKING_REASONS for reason in reasons)


def _blocks_last_resort_accept_for_candidate(
    q: dict,
    reasons: list[str],
    *,
    source_text: str | None = None,
    course_id: str | None = None,
) -> bool:
    if _blocks_last_resort_accept(reasons):
        return True

    if not is_csen603_git_workflow_scope(course_id, str(q.get("topic", "")), source_text):
        return False

    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    question_text = _git_question_text(q)
    question_blob = _git_text_blob(q)

    if topic_bucket == "branching_and_merging":
        if _git_branching_stem_is_overelaborate(question_text) or _git_branching_has_advanced_drift(question_blob):
            return True
        if "too_many_out_of_source_specifics" in reasons:
            return _git_family(q, course_id) not in {
                "branch_isolation",
                "branch_vs_trunk",
                "merge_commit_graph",
                "merge_conflict_resolution",
            }

    if topic_bucket == "commit_quality":
        return (
            "git_commit_quality_wrong_focus" in reasons
            or not _git_commit_quality_template_match(question_text, question_blob)
            or _git_family(q, course_id) != "commit_message_quality"
        )

    if topic_bucket == "distributed_vcs":
        return not _git_dvc_reserve_focus_ok(q)

    if not _is_fragile_git_topic_bucket(topic_bucket):
        return False

    if any(reason in GIT_FRAGILE_LAST_RESORT_REASONS for reason in reasons):
        return True

    if "too_many_out_of_source_specifics" in reasons and _git_fragile_topic_reserve_drift(q, course_id):
        return True

    return False


def _normalize_course_signal(course_id: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(course_id or "").strip().lower())


def _is_csen603_course(course_id: str | None) -> bool:
    return "csen603" in _normalize_course_signal(course_id)


def _topic_contains_any(topic: str, keywords: list[str]) -> bool:
    normalized_topic = " ".join(str(topic or "").strip().lower().split())
    return any(keyword in normalized_topic for keyword in keywords)


def _text_contains_any(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    return any(phrase in normalized for phrase in phrases)


def _count_text_phrase_matches(text: str, phrases: list[str] | tuple[str, ...]) -> int:
    normalized = " ".join(str(text or "").strip().lower().split())
    return sum(1 for phrase in phrases if phrase in normalized)


def _topic_is_design_or_pattern_family(topic: str) -> bool:
    return _topic_contains_any(
        topic,
        [
            "pattern", "solid", "design", "refactor", "principle",
            "abstraction", "information hiding", "coupling", "cohesion",
            "oop", "object oriented",
        ],
    )


def is_csen603_git_workflow_scope(
    course_id: str | None,
    topic: str,
    source_text: str | None,
) -> bool:
    if not _is_csen603_course(course_id):
        return False

    combined = " ".join([
        str(topic or ""),
        str(source_text or ""),
    ]).lower()

    repository_markers = [
        "git", "github", "version control", "repository", "repo",
        "distributed version control",
    ]
    workflow_markers = [
        "clone", "pull", "push", "commit", "branch", "merge",
        "merge conflict", "working copy", "remote repository",
        "continuous integration", "continuous delivery", "continuous deployment",
        "mainline", "trunk", "commit message",
    ]

    return _text_contains_any(combined, repository_markers) and _count_text_phrase_matches(combined, workflow_markers) >= 2


def _git_workflow_topic_hint(topic: str, source_text: str | None = None) -> str:
    topic_bucket = _git_topic_bucket(topic)
    combined = " ".join([str(topic or ""), str(source_text or "")]).lower()

    if topic_bucket == "branching_and_merging":
        return (
            "Prefer short exam-style branching questions about branch isolation, branch versus trunk or "
            "mainline, merge-commit graph interpretation, or one simple lecture-grounded merge-conflict "
            "case. Keep stems short, avoid rich project dressing, and avoid advanced workaround narratives "
            "such as cherry-pick, revert, rename-file handling, or silent post-merge bug stories."
        )

    if topic_bucket == "merge_conflicts":
        return (
            "Prefer merge-conflict questions that stay on conflict-aware workflow: remote ahead, "
            "sync first, overlapping edits, conflict after pull or merge, resolve conflicting files "
            "locally, then continue the normal commit and push flow. Do NOT imply that push itself "
            "directly creates a merge conflict or that remote history is overwritten automatically."
        )

    if topic_bucket == "commit_quality":
        return (
            "Prefer commit-message-quality questions that stay on one short message-quality scenario or one "
            "pair of commit messages. Focus on clarity, specificity, what changed, why it changed, and "
            "usefulness to teammates, reviewers, and project history. Do NOT drift into push or pull "
            "workflow, merge-conflict handling, branch isolation, low-level repair commands, or GitHub review tooling."
        )

    if topic_bucket == "distributed_vcs":
        return (
            "Prefer distributed-version-control reasoning about each developer having a full local copy or "
            "local repository, local history, offline work, and later synchronization with a remote. Do NOT "
            "drift into merge-conflict handling, branch isolation, CI or CD, or routine pull-before-push basics."
        )

    if topic_bucket == "ci_cd_fundamentals":
        return (
            "Prefer CI/CD questions about commit-triggered build and test behavior, self-testing builds, "
            "keeping the build fast, every commit building on the integration machine, and continuous "
            "delivery versus continuous deployment. Do NOT drift into branch isolation, clone or pull basics, "
            "merge-conflict handling, or generic remote-ahead workflow."
        )

    if _text_contains_any(combined, ["clone", "local working copy", "local repository", "remote repository", "pull", "push"]):
        return (
            "Prefer short workflow scenarios that distinguish clone, pull, commit, and push "
            "based on whether the developer already has a local repository and whether teammates "
            "need to see the change remotely."
        )

    if _text_contains_any(combined, ["merge conflict", "merge", "remote is ahead", "overlapping edits"]):
        return (
            "Prefer two-developer scenarios about remote-ahead push rejection, merge, and "
            "merge-conflict resolution grounded in repository history."
        )

    if _text_contains_any(combined, ["branch", "trunk", "mainline", "main branch"]):
        return (
            "Prefer isolated feature-work versus shared baseline scenarios that distinguish "
            "branching from trunk or mainline work."
        )

    if _text_contains_any(combined, ["commit", "commit message", "push"]):
        return (
            "Prefer local-versus-shared visibility and commit-message-quality scenarios rather "
            "than command trivia."
        )

    if _text_contains_any(combined, ["continuous integration", "continuous delivery", "continuous deployment", "ci", "cd"]):
        return (
            "Prefer repository-triggered build, test, release, and delivery-versus-deployment "
            "discrimination without tool syntax or web-UI details."
        )

    if _text_contains_any(combined, ["distributed version control", "configuration management", "repository history"]):
        return (
            "Prefer conceptual workflow discrimination grounded in collaboration, version history, "
            "and local-versus-remote repository behavior."
        )

    return (
        "Prefer short team workflow scenarios about local versus remote state, collaboration, "
        "branching, merging, and CI/CD consequences."
    )


def _git_workflow_difficulty_hint(topic: str, difficulty: int, generation_profile: str) -> str:
    topic_bucket = _git_topic_bucket(topic)
    if generation_profile == "diagnostic":
        return (
            "Diagnostic tone: keep it clean and answerable. Prefer modest workflow discrimination "
            "such as clone versus pull, commit versus push, or local versus remote."
        )

    if difficulty <= 2:
        return (
            "Difficulty 1-2: prefer clean command or concept discrimination such as clone versus pull, "
            "commit versus push, branch versus trunk, or local versus remote."
        )
    if difficulty == 3:
        return "Difficulty 3: prefer short two-developer workflow scenarios."
    if topic_bucket == "branching_and_merging":
        return (
            "Difficulty 4-5: keep the branching question short and exam-style. Prefer branch isolation, "
            "branch versus trunk or mainline, merge-graph interpretation, or one simple merge-conflict case."
        )
    if topic_bucket == "distributed_vcs":
        return (
            "Difficulty 4-5: keep the DVC question conceptual and focused on full local copy, local history, "
            "offline work, and later synchronization, not on generic remote-update workflow."
        )
    if topic_bucket == "commit_quality":
        return (
            "Difficulty 4-5: keep the Commit Quality question short and tightly anchored to commit-message "
            "specificity, what changed, why it changed, and usefulness to review or history."
        )
    return (
        "Difficulty 4-5: prefer merge or conflict reasoning, commit-visibility traps, CI/CD discrimination, "
        "or multi-step workflow reasoning that remains lecture-grounded."
    )


def _git_topic_micro_profile(topic: str) -> str:
    topic_bucket = _git_topic_bucket(topic)
    if topic_bucket == "branching_and_merging":
        return (
            "Git subtopic micro-profile: Branching and Merging\n"
            "- Focus on branch isolation, branch versus trunk or mainline, merge-commit graph meaning, and only simple lecture-grounded merge-conflict reasoning.\n"
            "- Keep hard stems to one or two short sentences when possible.\n"
            "- Do NOT use elaborate project deadlines, competing team constraints, critical shared config stories, cherry-pick or revert narratives, rename-file workarounds, or rich post-merge bug stories."
        )
    if topic_bucket == "merge_conflicts":
        return (
            "Git subtopic micro-profile: Merge Conflicts\n"
            "- Focus on remote ahead, overlapping edits, conflict after pull or merge, local conflict resolution, and then continuing the normal commit or push flow.\n"
            "- Do NOT imply that push itself creates the conflict, that remote history is overwritten automatically, or that the conflict resolves itself."
        )
    if topic_bucket == "commit_quality":
        return (
            "Git subtopic micro-profile: Commit Quality\n"
            "- HARD TEMPLATE: use only a short commit-message-quality shape.\n"
            "- Allowed shapes: compare a vague versus specific commit message, ask what a good commit message should communicate, or ask how vague messages hurt review, history, or maintenance.\n"
            "- Focus on clear, specific, useful commit messages that describe what changed and why, and that help teammates, reviewers, and future history-reading.\n"
            "- Keep the stem short, prefer one short scenario or one pair of messages, and avoid project-heavy workflow stories.\n"
            "- Do NOT make push or pull workflow, merge conflicts, branch isolation, CI/CD, pull requests, GitHub review tooling, or low-level repair commands the main concept."
        )
    if topic_bucket == "distributed_vcs":
        return (
            "Git subtopic micro-profile: Distributed Version Control\n"
            "- Focus on each developer having a full local repository and history, offline work, and later synchronization with a remote.\n"
            "- Prefer concept focus such as distributed VCS, full local copy, local history, and offline work.\n"
            "- Do NOT make merge-conflict resolution, branch isolation, CI or CD, or ordinary local-update workflow the main concept."
        )
    if topic_bucket == "ci_cd_fundamentals":
        return (
            "Git subtopic micro-profile: CI and CD Fundamentals\n"
            "- Focus on commit-triggered build and test behavior, self-testing builds, keeping the build fast, every commit building on the integration machine, and delivery versus deployment.\n"
            "- Do NOT turn the question into branch isolation, clone or pull basics, merge-conflict handling, or remote-ahead workflow."
        )
    return ""


def _build_git_workflow_style_block(
    course_id: str | None,
    topic: str,
    difficulty: int,
    generation_profile: str,
    source_text: str | None,
) -> str:
    if not is_csen603_git_workflow_scope(course_id, topic, source_text):
        return ""

    topic_bucket = _git_topic_bucket(topic)
    topic_hint = _git_workflow_topic_hint(topic, source_text)
    difficulty_hint = _git_workflow_difficulty_hint(topic, difficulty, generation_profile)
    micro_profile = _git_topic_micro_profile(topic)
    extra_guardrail = ""
    if topic_bucket == "commit_quality":
        extra_guardrail = (
            "\n- Commit Quality hard template: choose only one of these shapes: "
            "(1) compare a vague versus specific commit message, "
            "(2) ask what a good commit message should communicate about what changed and why, or "
            "(3) ask how a vague message hurts review, history, or maintenance."
            "\n- For Commit Quality, keep the stem short, prefer one short scenario or one pair of messages, "
            "avoid project-heavy nouns unless they are harmless example text, and do NOT make push, pull, "
            "merge, branches, pull requests, CI/CD, or repair commands the main concept."
        )
    elif topic_bucket == "branching_and_merging":
        extra_guardrail = (
            "\n- For Branching and Merging, keep hard stems to one or two sentences when possible."
            "\n- Prefer branch isolation, branch versus trunk or mainline, merge-commit graph meaning, "
            "or one simple merge-conflict case."
        )
    elif topic_bucket == "distributed_vcs":
        extra_guardrail = (
            "\n- For Distributed Version Control, keep the question centered on full local copy, local history, "
            "offline work, and later synchronization with a remote."
        )
    return f"""

CSEN603 Git workflow lecture profile:
- Stay strictly within Git or GitHub workflow and software-engineering collaboration scope grounded in the provided lecture text.
- Do NOT ask about GitHub web UI or product trivia such as pull-request pages, issues, stars, forks pages, Actions YAML syntax, repo settings, README details, or interface details.
- Prefer short exam-style MCQs about clone versus pull, commit versus push, local working copy versus remote repository, branch versus trunk or mainline, merge and merge conflicts, distributed version control, commit-message quality, CI after commit, and continuous delivery versus continuous deployment.
- Prefer coverage across distinct Git workflow families such as clone-first checkout, pull remote updates, commit versus push visibility, branch isolation, branch versus trunk, remote-ahead push rejection, merge conflict resolution, merge-commit graph interpretation, commit-message quality, CI per commit, delivery versus deployment, and workflow-sequence reasoning.
- Prefer mini team scenarios with named developers when that stays grounded in the lecture text.
- Vary stem shapes across questions. Use forms such as what went wrong, what should happen next, which workflow mistake occurred, which sequence is correct, what does the commit graph imply, which action best isolates risky work, or which practice helps the team most.
- Vary actor templates. Sometimes use named developers, but also use a teammate, a maintainer, a new developer, a contributor, the local branch, or the remote repository. Do NOT overuse Alice or Bob style pairs.
- Prefer one clearly best answer rather than multiple broadly correct workflow statements.
- Git workflow topic hint: {topic_hint}
- Git workflow micro-profile:
{micro_profile or "- No special Git subtopic micro-profile beyond the current lecture scope."}
- Git workflow difficulty guidance: {difficulty_hint}
- Topic-specific generation guardrail:{extra_guardrail or " None beyond the current Git lecture scope."}
- For hard and very hard Git questions, avoid collapsing into repeated basic clone-versus-pull discrimination. Prefer remote-ahead push rejection, merge/conflict reasoning, commit-versus-push visibility traps, merge-graph interpretation, CI/CD discrimination, or workflow-sequence reasoning when the lecture text supports them.
- Keep the answer inferable from the lecture text only.
""".rstrip()


def _build_hard_question_quality_block(
    course_id: str | None,
    topic: str,
    difficulty: int,
    generation_profile: str,
) -> str:
    if difficulty < 4:
        return ""

    if generation_profile == "diagnostic":
        lines = [
            "Hard diagnostic quality gate:",
            "- Keep the question clean and answerable, but do NOT make it plain slogan or definition recall with harder wording.",
            "- Prefer a short applied discrimination, correction, or best-choice-in-context over a generic benefit or main-goal stem.",
            "- If the stem clearly points to a named concept, principle, or pattern, the intended option must explicitly appear among the answer choices.",
        ]
    else:
        lines = [
            "Hard-question quality gate:",
            "- Do NOT write a hard or very hard question that is only definition recall, slogan recall, or a principle tagline with harder wording.",
            "- Avoid generic stems like 'What is the primary benefit of...', 'What is the key characteristic of...', or 'What is the main goal of...' unless the options are sharply separated and only one answer is clearly best.",
            "- If the stem clearly points to a named concept, principle, or pattern, the intended option must explicitly appear among the answer choices.",
            "- Prefer a short concrete situation, tradeoff, violation, smell, correction, refactor, or best-choice-in-context over a generic benefit or slogan question.",
        ]

    if _is_csen603_course(course_id) and _topic_is_design_or_pattern_family(topic):
        lines.append(
            "- For CSEN603 design or pattern topics, do NOT ask a hard question that merely recalls a design-pattern or SOLID slogan. Use a short design situation, violation, tradeoff, or refactoring choice instead."
        )

    return "\n" + "\n".join(lines)


def _csen603_style_hint(topic: str, difficulty: int, generation_profile: str) -> str:
    topic_text = str(topic or "").strip().lower()
    style_map = [
        (
            [
                "requirement", "use case", "stakeholder", "user story",
                "verifiab", "include", "extend", "nfr", "fr",
            ],
            (
                "Prefer short stakeholder, functional-vs-non-functional classification, "
                "verifiability check, include-vs-extend, user-story quality, dependency, "
                "or ambiguity scenarios. Avoid pure textbook-definition stems when a "
                "classification or issue-identification scenario is possible."
            ),
        ),
        (
            [
                "architecture", "rest", "soa", "service", "api",
                "stateless", "stateful", "redundan", "failure", "boundary",
            ],
            (
                "Prefer architecture matching, tradeoff, redundancy, single-point-of-failure, "
                "service-boundary, stateless-vs-stateful, or best-fit architecture choice "
                "scenarios. Avoid generic definition stems when a short constraint-based "
                "situation can be used."
            ),
        ),
        (
            [
                "ui", "ux", "usability", "heuristic", "accessib",
                "cta", "feedback", "interaction", "interface",
            ],
            (
                "Prefer violated-principle, redesign, visible call-to-action, visible state "
                "or feedback, error-handling, accessibility, recognition-vs-recall, or "
                "memory-load scenarios. Avoid generic 'what is usability engineering' style "
                "stems unless difficulty is very low."
            ),
        ),
        (
            [
                "pattern", "solid", "design", "refactor", "extensib",
                "single responsibility", "open closed", "liskov",
                "interface segregation", "dependency inversion",
            ],
            (
                "Prefer pattern identification from a short situation, role assignment, "
                "extension or refactoring, SOLID-violation identification, or best-design-choice "
                "scenarios. Avoid simple definition recall when a small design situation can be used."
            ),
        ),
        (
            [
                "diagram", "uml", "model", "multiplicity", "relation",
                "abstraction", "class diagram", "sequence diagram",
                "activity diagram", "state diagram",
            ],
            (
                "Prefer correction, wrong relation or multiplicity, abstraction mismatch, "
                "or extensibility or modeling-improvement scenarios. Avoid purely naming "
                "diagram types unless difficulty is very low."
            ),
        ),
        (
            [
                "testing", "test", "verification", "validation", "coverage",
                "integration", "stub", "driver",
            ],
            (
                "Prefer test-type classification, driver-vs-stub, integration strategy choice, "
                "boundary or partition reasoning, coverage reasoning, or applied testing scenarios. "
                "Avoid generic definition stems unless difficulty is very low."
            ),
        ),
        (
            [
                "agile", "scrum", "sprint", "velocity", "burndown",
                "kano", "moscow", "backlog", "ceremony",
            ],
            (
                "Prefer role identification, sprint planning or allocation, prioritization, "
                "ceremony interpretation, burndown, backlog, or story-point scenarios. "
                "Avoid pure manifesto-style definition questions unless difficulty is very low."
            ),
        ),
        (
            [
                "maintenance", "git", "ci", "cd", "smell",
                "version control", "continuous integration", "continuous delivery",
            ],
            (
                "Prefer workflow or collaboration situations, smell identification, "
                "refactoring or maintenance-type classification, version-control interpretation, "
                "or CI/CD interpretation scenarios. Avoid overly tool-specific trivia not grounded "
                "in the lecture text."
            ),
        ),
    ]

    for keywords, hint in style_map:
        if _topic_contains_any(topic_text, keywords):
            return hint

    return (
        "Prefer a short realistic software-engineering scenario, classification, best-choice, "
        "or issue-identification stem over a pure definition only when the lecture text supports it."
    )


def _csen603_difficulty_style_hint(difficulty: int, generation_profile: str) -> str:
    if generation_profile == "diagnostic":
        return (
            "Diagnostic tone: keep the question clean, answerable, and modestly applied. "
            "Prefer short classification or simple applied identification over a generic definition "
            "when the topic allows it, but avoid tricky or heavy multi-step reasoning."
        )

    if difficulty <= 1:
        return (
            "Difficulty 1: keep it very answerable. Prefer concrete applied classification over raw "
            "definition when possible, but do NOT make it reasoning-heavy."
        )
    if difficulty == 2:
        return (
            "Difficulty 2: prefer a simple short scenario or direct applied identification. "
            "Avoid pure textbook-definition stems if a cleaner applied stem exists."
        )
    if difficulty == 3:
        return (
            "Difficulty 3: make the scenario style clearly visible. Prefer short applied situations, "
            "classification, best choice, or redesign selection."
        )
    if difficulty == 4:
        return (
            "Difficulty 4: prefer applied reasoning, best answer among plausible options, tradeoff, "
            "correction, or interpretation."
        )
    return (
        "Difficulty 5: prefer nuanced but still lecture-grounded scenarios with plausible alternatives, "
        "without adding unsupported external details."
    )


def _csen603_stem_guardrail_hint(difficulty: int, generation_profile: str) -> str:
    if generation_profile == "diagnostic":
        return (
            "Still avoid generic stems like 'What is the primary goal of...' when a short applied or "
            "classification form is cleanly possible."
        )
    if difficulty <= 2:
        return (
            "Avoid generic stems like 'What is the primary goal of...' or 'Which principle should be prioritized?' "
            "when a cleaner applied classification or identification form is available, but keep low difficulty simple."
        )
    return (
        "Strongly discourage generic stems like 'What is the primary goal of...', "
        "'Which principle should be prioritized?', or 'What is likely to happen if...' unless the lecture "
        "topic truly only supports that style."
    )


def _build_course_style_block(
    course_id: str | None,
    topic: str,
    difficulty: int,
    generation_profile: str,
    source_text: str | None = None,
) -> str:
    blocks: list[str] = []

    if _is_csen603_course(course_id):
        style_hint = _csen603_style_hint(topic, difficulty, generation_profile)
        difficulty_hint = _csen603_difficulty_style_hint(difficulty, generation_profile)
        stem_guardrail_hint = _csen603_stem_guardrail_hint(difficulty, generation_profile)
        blocks.append(f"""

CSEN603 exam-style framing:
- Prefer scenario-based MCQs over pure definitions when the lecture text supports it.
- Prefer short applied scenarios, classification, issue-identification, best-method or best-pattern or best-test or best-design-choice, redesign, correction, or improvement questions whenever the lecture text supports them.
- Use realistic but minimal software engineering situations grounded in the provided lecture text, such as a small team situation, short UI behavior, short architecture or design constraint, short testing or integration situation, short sprint or planning scenario, or short maintenance or version-control situation.
- Favor issue identification, concept classification, choosing the best method, pattern, or testing approach, design repair or improvement, interpreting a short concrete situation, and justifying the best answer among plausible alternatives.
- Topic-family style hint: {style_hint}
- Difficulty-sensitive guidance: {difficulty_hint}
- Stem preference guardrail: {stem_guardrail_hint}
- You may frame the question as a realistic software engineering scenario.
- However, the correct answer must still be clearly inferable from the provided lecture text.
- Do NOT rely on external assignment text, exam text, tool, platform, company, or workflow details not supported by the source.
- Do NOT invent GitHub-specific, Jira-specific, CI/CD platform-specific, or deployment-specific behavior unless the lecture text clearly supports that level of detail.
- Keep the scenario minimal. If an applied framing would become unsupported or overly complex, choose the safest grounded applied form available.
- If the lecture text does not support a realistic scenario cleanly, prefer a grounded conceptual or applied question instead.
""".rstrip())

    git_workflow_block = _build_git_workflow_style_block(
        course_id=course_id,
        topic=topic,
        difficulty=difficulty,
        generation_profile=generation_profile,
        source_text=source_text,
    )
    if git_workflow_block:
        blocks.append(git_workflow_block)

    return "\n\n".join(blocks)


def _build_hard_short_scope_policy(
    *,
    difficulty: int,
    context_mode: str,
    selected_context: str,
    selected_chunk_count: int,
    max_provider_model_attempts: int | None,
) -> dict[str, Any]:
    selected_chars = len(selected_context or "")
    is_high_difficulty = difficulty >= 4
    is_short_scope = selected_chars <= HARD_SHORT_SCOPE_MAX_CHARS
    is_narrow_context = (
        context_mode == "full_cleaned_text"
        or selected_chunk_count <= HARD_SHORT_SCOPE_MAX_SEED_CHUNKS
    )
    risky_generation = is_high_difficulty and is_short_scope and is_narrow_context

    effective_attempt_budget = max_provider_model_attempts
    if risky_generation:
        if effective_attempt_budget is None:
            effective_attempt_budget = HARD_SHORT_SCOPE_MAX_PROVIDER_MODEL_ATTEMPTS
        else:
            effective_attempt_budget = min(
                effective_attempt_budget,
                HARD_SHORT_SCOPE_MAX_PROVIDER_MODEL_ATTEMPTS,
            )

    return {
        "risky_generation": risky_generation,
        "selected_chars": selected_chars,
        "context_mode": context_mode,
        "selected_chunk_count": selected_chunk_count,
        "effective_attempt_budget": effective_attempt_budget,
    }


def _build_no_valid_question_context(
    *,
    policy: dict[str, Any],
    requested_difficulty: int,
    provider_model_attempts: int,
    brittle_validation_failures: int,
    provider_failures: int,
    rate_limit_failures: int,
    exit_reason: str,
) -> dict[str, Any]:
    return {
        "risky_generation": bool(policy.get("risky_generation")),
        "requested_difficulty": requested_difficulty,
        "selected_chars": policy.get("selected_chars"),
        "context_mode": policy.get("context_mode"),
        "selected_chunk_count": policy.get("selected_chunk_count"),
        "effective_attempt_budget": policy.get("effective_attempt_budget"),
        "provider_model_attempts": provider_model_attempts,
        "brittle_validation_failures": brittle_validation_failures,
        "provider_failures": provider_failures,
        "rate_limit_failures": rate_limit_failures,
        "exit_reason": exit_reason,
    }


def _fallback_difficulty_candidates(
    requested_difficulty: int,
    attempted_difficulties: list[int],
) -> list[int]:
    if requested_difficulty == 5:
        candidates = [4, 3]
    elif requested_difficulty == 4:
        candidates = [3]
    else:
        candidates = []

    return [difficulty for difficulty in candidates if difficulty not in attempted_difficulties]


def validate_question(
    q: dict,
    source_text: str | None = None,
    validation_profile: str = "default",
    course_id: str | None = None,
) -> tuple[bool, list[str]]:
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
    reasons.extend(
        _difficulty_mismatch_reasons(
            q,
            source_text,
            validation_profile=validation_profile,
            course_id=course_id,
        )
    )
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


def _provider_model_key(provider_name: str, model: str) -> str:
    return f"{provider_name}:{model}"


def _model_cooldown_remaining(provider_name: str, model: str) -> float:
    now = time.time()
    key = _provider_model_key(provider_name, model)
    expires_at = _MODEL_COOLDOWNS.get(key, 0.0)
    if expires_at <= now:
        _MODEL_COOLDOWNS.pop(key, None)
        return 0.0
    return expires_at - now


def _git_challenge_model_suppression_remaining(provider_name: str, model: str) -> float:
    now = time.time()
    key = _provider_model_key(provider_name, model)
    expires_at = _GIT_CHALLENGE_MODEL_SUPPRESSIONS.get(key, 0.0)
    if expires_at <= now:
        _GIT_CHALLENGE_MODEL_SUPPRESSIONS.pop(key, None)
        return 0.0
    return expires_at - now


def _set_git_challenge_model_suppression(
    provider_name: str,
    model: str,
    seconds: int,
    reason: str,
) -> None:
    seconds = max(1, int(seconds))
    now = time.time()
    key = _provider_model_key(provider_name, model)
    expires_at = max(_GIT_CHALLENGE_MODEL_SUPPRESSIONS.get(key, 0.0), now + seconds)
    _GIT_CHALLENGE_MODEL_SUPPRESSIONS[key] = expires_at
    logger.info(
        "[LLM] Git challenge model suppression provider=%s model=%s seconds=%s reason=%s",
        provider_name,
        model,
        int(expires_at - now),
        reason,
    )


def _record_git_challenge_model_instability(
    provider_name: str,
    model: str,
    failure_kind: str,
) -> None:
    now = time.time()
    key = f"{_provider_model_key(provider_name, model)}:{failure_kind}"
    events = [stamp for stamp in _MODEL_INSTABILITY_EVENTS.get(key, []) if now - stamp <= 900]
    events.append(now)
    _MODEL_INSTABILITY_EVENTS[key] = events
    if len(events) < 2:
        return

    seconds = 900
    if provider_name == "fireworks" and model == "accounts/fireworks/models/deepseek-v3p2" and failure_kind == "json_parse_failure":
        seconds = 1800
    elif provider_name == "mistral" and failure_kind == "upstream_503_overflow":
        seconds = 1800
    elif failure_kind == "read_timeout":
        seconds = 1200

    _set_git_challenge_model_suppression(provider_name, model, seconds, failure_kind)


def _set_provider_cooldown(provider_name: str, seconds: int) -> None:
    seconds = max(1, int(seconds))
    now = time.time()
    _PROVIDER_COOLDOWNS[provider_name] = max(
        _PROVIDER_COOLDOWNS.get(provider_name, 0.0),
        now + seconds,
    )


def _set_model_cooldown(provider_name: str, model: str, seconds: int, reason: str) -> None:
    seconds = max(1, int(seconds))
    now = time.time()
    key = _provider_model_key(provider_name, model)
    expires_at = max(_MODEL_COOLDOWNS.get(key, 0.0), now + seconds)
    _MODEL_COOLDOWNS[key] = expires_at
    logger.info(
        "[LLM] Cooldown set provider=%s model=%s seconds=%s reason=%s",
        provider_name,
        model,
        int(expires_at - now),
        reason,
    )


def _extract_retry_after_seconds(message: str) -> int | None:
    if not message:
        return None

    match = re.search(
        r"\bin\s+(?:(?P<minutes>\d+(?:\.\d+)?)m)?\s*(?P<seconds>\d+(?:\.\d+)?)s\b",
        message,
        re.IGNORECASE,
    )
    if match:
        minutes = float(match.group("minutes") or 0)
        seconds = float(match.group("seconds") or 0)
        total_seconds = minutes * 60 + seconds
        return max(1, math.ceil(total_seconds))

    match = re.search(r"\bin\s+(?P<minutes>\d+(?:\.\d+)?)m\b", message, re.IGNORECASE)
    if match:
        minutes = float(match.group("minutes"))
        return max(1, math.ceil(minutes * 60))

    return None


def _is_openrouter_daily_limit(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "free-models-per-day",
            "free models per day",
            "daily limit",
            "daily quota",
            "quota exceeded",
            "per day",
        )
    )


def _record_failure_cooldown(
    provider_name: str,
    model: str,
    status_code: int,
    message: str,
) -> None:
    retry_after = _extract_retry_after_seconds(message)
    lowered = message.lower()

    if provider_name == "gemini":
        now = time.time()
        if status_code == 429:
            _set_provider_cooldown(provider_name, 600)
            logger.info(
                "[LLM] Provider cooldown set provider=%s seconds=%s reason=%s",
                provider_name,
                600,
                "rate_limit_429",
            )
            return

        if status_code != 503:
            return

        failures = _PROVIDER_503_FAILURES.get(provider_name, [])
        failures = [stamp for stamp in failures if now - stamp <= 120]
        failures.append(now)
        _PROVIDER_503_FAILURES[provider_name] = failures
        if len(failures) >= 2:
            _set_provider_cooldown(provider_name, 120)
            logger.info(
                "[LLM] Provider cooldown set provider=%s seconds=%s reason=%s",
                provider_name,
                120,
                "repeated_503",
            )
        return

    if provider_name == "cerebras" and status_code == 404 and (
        "model_not_found" in lowered or "not found" in lowered
    ):
        logger.info(
            "[LLM] Long model cooldown provider=%s model=%s seconds=%s reason=%s",
            provider_name,
            model,
            86400,
            "model_not_found",
        )
        _set_model_cooldown(provider_name, model, 86400, "model_not_found")
        return

    if status_code == 429:
        if provider_name == "groq":
            if model == "llama-3.3-70b-versatile":
                seconds = retry_after if retry_after is not None else 900
                logger.info(
                    "[LLM] Long model cooldown provider=%s model=%s seconds=%s reason=%s",
                    provider_name,
                    model,
                    seconds,
                    "rate_limit_429",
                )
            elif model == "llama-3.1-8b-instant":
                seconds = retry_after if retry_after is not None else 30
            else:
                seconds = retry_after if retry_after is not None else 60
            _set_model_cooldown(provider_name, model, seconds, "rate_limit_429")
            return

        if provider_name == "openrouter":
            if _is_openrouter_daily_limit(message):
                seconds = retry_after if retry_after is not None else 21600
                logger.info(
                    "[LLM] Long model cooldown provider=%s model=%s seconds=%s reason=%s",
                    provider_name,
                    model,
                    seconds,
                    "daily_limit_429",
                )
                _set_model_cooldown(provider_name, model, seconds, "daily_limit_429")
            else:
                seconds = retry_after if retry_after is not None else 120
                _set_model_cooldown(provider_name, model, seconds, "rate_limit_429")
            return

        seconds = retry_after if retry_after is not None else 60
        _set_model_cooldown(provider_name, model, seconds, "rate_limit_429")
        return

    if status_code == 503:
        seconds = retry_after if retry_after is not None else 60
        _set_model_cooldown(provider_name, model, seconds, "upstream_503")
        return


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

    for attempt in range(2):
        logger.info(
            "[LLM] Request -> provider=%s model=%s attempt=%s",
            provider_name,
            model,
            attempt + 1,
        )

        resp = await client.post(endpoint, headers=headers, json=payload)

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code >= 400:
            msg = _extract_error_message(data)
            excerpt = _response_excerpt(resp)
            full_message = f"{msg} | body={excerpt}"
            retry_after = _extract_retry_after_seconds(full_message)

            if (
                attempt == 0
                and resp.status_code in {429, 503}
                and retry_after is not None
                and retry_after <= 8
            ):
                logger.info(
                    "[LLM] Short retry provider=%s model=%s wait=%ss status=%s",
                    provider_name,
                    model,
                    retry_after,
                    resp.status_code,
                )
                await asyncio.sleep(retry_after)
                continue

            raise ProviderHTTPError(
                provider=provider_name,
                model=model,
                status_code=resp.status_code,
                message=full_message,
            )

        content = _extract_message_content(data)
        logger.info(f"[LLM] Response OK <- provider={provider_name} model={model}")
        return content

    raise RuntimeError(f"Model call unexpectedly exhausted retries for {provider_name}/{model}")

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
    "attendance policy",
    "submission instructions",
    "grading policy",
    "grading weight",
    "exam date",
]

ADMIN_LINE_MARKERS = [
    "office hours",
    "attendance policy",
    "submission instructions",
    "submission deadline",
    "due date",
    "grading weight",
    "grading policy",
    "exam date",
    "instructor email",
    "ta email",
]

FORBIDDEN_QUESTION_MARKERS = [
    "office hours",
    "piazza",
    "access code",
    "email",
    "course grade",
    "final exam",
    "midterm exam",
    "project submission",
    "project deadline",
    "project report",
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

GENERIC_REASONING_TERMS = {
    "approach", "approaches", "between", "characteristic", "characteristics",
    "comparing", "considering", "dependency", "dependencies", "despite",
    "primary", "relationship", "relationships", "tradeoff", "tradeoffs",
}

DIAGNOSTIC_GENERIC_REASONING_TERMS = GENERIC_REASONING_TERMS | {
    "applied", "benefit", "benefits", "commitment", "commitments",
    "completed", "consideration", "considerations", "describe",
    "describes", "flexible",
}


def _provider_order_for_generation(generation_profile: str = "default") -> list[str]:
    global _PROVIDER_RUNTIME_AUDIT_LOGGED
    if not _PROVIDER_RUNTIME_AUDIT_LOGGED:
        _provider_registry_audit()
        _PROVIDER_RUNTIME_AUDIT_LOGGED = True

    if generation_profile == "git_challenge":
        return list(GIT_CHALLENGE_SAFE_PROVIDER_ORDER)

    if generation_profile != "diagnostic":
        return list(PROVIDER_ORDER)

    ordered = []
    seen = set()
    for provider_name in DIAGNOSTIC_PROVIDER_PRIORITY + PROVIDER_ORDER:
        if provider_name in seen:
            continue
        seen.add(provider_name)
        ordered.append(provider_name)
    return ordered


def _provider_models_for_generation(provider_name: str, generation_profile: str = "default") -> list[str]:
    models = list(PROVIDER_MODELS.get(provider_name, []))
    if generation_profile == "git_challenge":
        preferred = GIT_CHALLENGE_SAFE_MODEL_PREFERENCES.get(provider_name, [])
        if not preferred:
            return []
        ordered = []
        seen = set()
        for model in preferred + models:
            if model in seen or model not in models:
                continue
            seen.add(model)
            ordered.append(model)
        return ordered

    if generation_profile != "diagnostic":
        return models

    preferred = DIAGNOSTIC_PROVIDER_MODEL_PREFERENCES.get(provider_name, [])
    if not preferred:
        return models

    ordered = []
    seen = set()
    for model in preferred + models:
        if model in seen or model not in models:
            continue
        seen.add(model)
        ordered.append(model)
    return ordered


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
            if any(marker in lowered for marker in ADMIN_LINE_MARKERS):
                continue
            if (
                ("submission" in lowered or "submit" in lowered)
                and any(marker in lowered for marker in ("assignment", "project", "quiz", "exam", "report"))
            ):
                continue
            if (
                any(marker in lowered for marker in ("deadline", "due date", "late penalty", "late submission"))
                and any(marker in lowered for marker in ("assignment", "project", "quiz", "exam", "report", "submission"))
            ):
                continue
            if ("grading" in lowered or "weight" in lowered) and "%" in lowered:
                continue
            if any(marker in lowered for marker in ("grading rubric", "grading breakdown", "attendance is mandatory")):
                continue
            if (
                any(marker in lowered for marker in ("exam", "quiz"))
                and any(marker in lowered for marker in ("scheduled", "opens", "closes", "room", "location"))
            ):
                continue
            if any(marker in lowered for marker in ("contact the instructor", "contact your instructor", "contact the ta")):
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


def _unsupported_specifics_breakdown(
    q: dict,
    source_text: str,
    *,
    generic_terms: set[str] | None = None,
    course_id: str | None = None,
) -> dict:
    generic_terms = generic_terms or GENERIC_REASONING_TERMS
    allowed_terms = _allowed_domain_terms_for_question(str(q.get("topic", "")), source_text)
    allowed_terms |= _git_safe_vocabulary_terms(
        q,
        source_text,
        course_id=course_id,
    )
    stem_terms = _significant_terms(str(q.get("question", "")))
    stem_unsupported = stem_terms - allowed_terms
    stem_filtered_generic = stem_unsupported & generic_terms
    counted_stem_terms = stem_unsupported - generic_terms

    option_unsupported_terms = []
    option_filtered_generic_terms = []
    options_with_any_unsupported = 0
    option_specific_count = 0

    for option_text in (q.get("options", {}) or {}).values():
        unsupported = _significant_terms(str(option_text)) - allowed_terms
        filtered_generic = unsupported & generic_terms
        counted_terms = unsupported - generic_terms
        option_unsupported_terms.append(counted_terms)
        option_filtered_generic_terms.append(filtered_generic)
        if counted_terms:
            options_with_any_unsupported += 1
        if len(counted_terms) >= 2:
            option_specific_count += 1

    distinct_terms = set(counted_stem_terms)
    for terms in option_unsupported_terms:
        distinct_terms.update(terms)

    filtered_generic_terms = set(stem_filtered_generic)
    for terms in option_filtered_generic_terms:
        filtered_generic_terms.update(terms)

    total_mentions = len(counted_stem_terms) + sum(len(terms) for terms in option_unsupported_terms)

    return {
        "stem_terms": counted_stem_terms,
        "option_terms": option_unsupported_terms,
        "distinct_terms": distinct_terms,
        "options_with_any_unsupported": options_with_any_unsupported,
        "option_specific_count": option_specific_count,
        "total_mentions": total_mentions,
        "filtered_generic_terms": filtered_generic_terms,
    }


def _git_out_of_source_calibration_bucket(
    q: dict,
    source_text: str,
    course_id: str | None,
) -> str | None:
    if not is_csen603_git_workflow_scope(course_id, str(q.get("topic", "")), source_text):
        return None
    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    if topic_bucket in {"branching_and_merging", "distributed_vcs", "commit_quality"}:
        return topic_bucket
    return None


def _git_out_of_source_semantic_drift_terms(topic_bucket: str | None) -> set[str]:
    drift_phrases: dict[str | None, tuple[str, ...]] = {
        "branching_and_merging": (
            "deadline",
            "critical shared config",
            "competing priorities",
            "team lead",
            "incident",
            "production",
            "staging",
            "database",
            "authentication",
            "ticket",
            "jira",
            "cherry-pick",
            "cherry pick",
            "revert",
            "rename file",
            "workaround",
        ),
        "distributed_vcs": (
            "pull before pushing",
            "push rejected",
            "merge conflict",
            "feature branch",
            "branch isolation",
            "continuous integration",
            "continuous delivery",
            "continuous deployment",
            "pipeline",
            "pull request",
        ),
        "commit_quality": (
            "push",
            "pull",
            "merge conflict",
            "feature branch",
            "continuous integration",
            "continuous delivery",
            "continuous deployment",
            "pipeline",
            "pull request",
            "github",
            "git commit --amend",
            "amend",
            "recommit",
            "rebase",
        ),
    }
    terms: set[str] = set()
    for phrase in drift_phrases.get(topic_bucket, ()):
        terms.update(_significant_terms(phrase))
    return terms


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


def _introduces_too_many_out_of_source_specifics(
    q: dict,
    source_text: str,
    *,
    validation_profile: str = "default",
    course_id: str | None = None,
) -> bool:
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4 or not source_text:
        return False

    generic_terms = (
        DIAGNOSTIC_GENERIC_REASONING_TERMS
        if validation_profile == "diagnostic"
        else GENERIC_REASONING_TERMS
    )
    breakdown = _unsupported_specifics_breakdown(
        q,
        source_text,
        generic_terms=generic_terms,
        course_id=course_id,
    )
    stem_term_count = len(breakdown["stem_terms"])
    distinct_count = len(breakdown["distinct_terms"])
    option_specific_count = breakdown["option_specific_count"]
    options_with_any_unsupported = breakdown["options_with_any_unsupported"]
    total_mentions = breakdown["total_mentions"]
    calibration_bucket = _git_out_of_source_calibration_bucket(q, source_text, course_id)
    semantic_drift_terms = (
        breakdown["distinct_terms"] & _git_out_of_source_semantic_drift_terms(calibration_bucket)
        if calibration_bucket
        else set()
    )

    default_should_reject = (
        distinct_count >= 8
        and option_specific_count >= 2
        and (stem_term_count >= 2 or total_mentions >= 10)
    ) or (
        distinct_count >= 10
        and options_with_any_unsupported >= 3
        and total_mentions >= 12
    )

    if validation_profile == "diagnostic":
        should_reject = (
            distinct_count >= 9
            and option_specific_count >= 2
            and (stem_term_count >= 3 or total_mentions >= 11)
        ) or (
            distinct_count >= 11
            and options_with_any_unsupported >= 3
            and total_mentions >= 13
        )
        if default_should_reject and not should_reject:
            logger.info(
                "[DIAG] Diagnostic-specific leniency applied validator=%s topic=%s",
                "too_many_out_of_source_specifics",
                q.get("topic"),
            )
    else:
        should_reject = default_should_reject

    if calibration_bucket == "branching_and_merging":
        calibrated_should_reject = (
            len(semantic_drift_terms) >= 2
            and distinct_count >= 8
            and (stem_term_count >= 2 or total_mentions >= 10)
        ) or (
            _git_branching_stem_is_overelaborate(str(q.get("question", "")))
            and distinct_count >= 8
            and len(semantic_drift_terms) >= 1
        ) or (
            distinct_count >= 12
            and options_with_any_unsupported >= 3
            and total_mentions >= 14
        )
        if default_should_reject and not calibrated_should_reject:
            logger.info(
                "[LLM] Git branching out-of-source leniency topic=%s semantic_drift=%s",
                q.get("topic"),
                sorted(semantic_drift_terms)[:5],
            )
        should_reject = calibrated_should_reject
    elif calibration_bucket == "distributed_vcs":
        calibrated_should_reject = (
            len(semantic_drift_terms) >= 2
            and distinct_count >= 7
            and (stem_term_count >= 2 or total_mentions >= 9)
        ) or (
            distinct_count >= 12
            and option_specific_count >= 2
            and total_mentions >= 14
        )
        if default_should_reject and not calibrated_should_reject:
            logger.info(
                "[LLM] Git DVC out-of-source leniency topic=%s semantic_drift=%s",
                q.get("topic"),
                sorted(semantic_drift_terms)[:5],
            )
        should_reject = calibrated_should_reject
    elif calibration_bucket == "commit_quality":
        calibrated_should_reject = (
            len(semantic_drift_terms) >= 1
            and distinct_count >= 7
            and (stem_term_count >= 2 or total_mentions >= 9)
        ) or (
            distinct_count >= 11
            and option_specific_count >= 2
            and total_mentions >= 13
        )
        if default_should_reject and not calibrated_should_reject:
            logger.info(
                "[LLM] Git commit-quality out-of-source leniency topic=%s semantic_drift=%s",
                q.get("topic"),
                sorted(semantic_drift_terms)[:5],
            )
        should_reject = calibrated_should_reject

    if should_reject:
        logger.info(
            "[LLM] Out-of-source specifics topic=%s filtered_generic=%s counted_terms=%s semantic_drift=%s option_count=%s",
            q.get("topic"),
            sorted(breakdown["filtered_generic_terms"])[:5],
            sorted(breakdown["distinct_terms"])[:5],
            sorted(semantic_drift_terms)[:5],
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


def _diagnostic_allows_borderline_distractors(q: dict) -> bool:
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

    correct_len = len(correct_text)
    anchor_terms = (
        set(_topic_terms(str(q.get("topic", ""))))
        | _significant_terms(str(q.get("question", "")))
        | _significant_terms(correct_text)
    )
    anchored_distractors = sum(
        1
        for distractor in distractors
        if anchor_terms & _significant_terms(distractor)
    )
    short_distractors = sum(
        1
        for distractor in distractors
        if len(distractor.split()) <= 3 or len(distractor) < max(12, int(correct_len * 0.45))
    )

    if anchored_distractors >= 2 and short_distractors <= 2:
        return True

    average_length = sum(len(text) for text in distractors) / len(distractors)
    return anchored_distractors >= 1 and average_length >= correct_len * 0.55


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


def _generic_benefit_stem_markers() -> tuple[str, ...]:
    return (
        "primary benefit",
        "main benefit",
        "key benefit",
        "primary goal",
        "main goal",
        "key characteristic",
        "main characteristic",
        "main purpose",
        "primary purpose",
        "which principle should be prioritized",
        "which principle is most important",
    )


def _broad_positive_option_markers() -> tuple[str, ...]:
    return (
        "flexibility",
        "extensibility",
        "maintainability",
        "reusability",
        "scalability",
        "modularity",
        "testability",
        "usability",
        "reliability",
        "readability",
        "simplicity",
        "consistency",
        "adaptability",
        "decoupling",
        "low coupling",
        "high cohesion",
        "abstraction",
        "information hiding",
        "encapsulation",
        "improve",
        "improves",
        "increase",
        "increases",
        "reduce",
        "reduces",
        "enhance",
        "enhances",
    )


def _slogan_recall_stem_markers() -> tuple[str, ...]:
    return (
        "which principle states",
        "which principle emphasizes",
        "which principle suggests",
        "which principle says",
        "what is the main idea of",
        "what does the principle of",
        "which design principle emphasizes",
        "which principle is described by",
    )


def _normalized_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _option_texts(q: dict) -> list[str]:
    return [
        str(text).strip()
        for text in (q.get("options", {}) or {}).values()
        if str(text).strip()
    ]


def _stem_asks_for_named_concept_choice(question_text: str) -> bool:
    text = _normalized_text(question_text)
    if not text:
        return False

    direct_markers = (
        "which pattern",
        "what pattern",
        "which design pattern",
        "which principle",
        "what principle",
        "which concept",
        "what concept",
        "which term",
        "what term",
        "which approach",
        "which design choice",
        "best identifies",
        "best matches",
        "best fits",
        "is the clearest example of",
    )
    if any(marker in text for marker in direct_markers):
        return True

    return (
        "best describes" in text
        and any(marker in text for marker in ("pattern", "principle", "concept", "design"))
    )


def _stem_implies_named_concept(question_text: str) -> str | None:
    text = _normalized_text(question_text)
    matches = []
    for canonical, aliases in NAMED_CONCEPT_ALIASES.items():
        matched_aliases = [alias for alias in aliases if alias in text]
        if matched_aliases:
            matches.append((canonical, max(len(alias) for alias in matched_aliases)))

    if not matches:
        return None

    matches.sort(key=lambda item: item[1], reverse=True)
    return matches[0][0]


def _options_include_named_concept(q: dict, canonical: str) -> bool:
    aliases = list(NAMED_CONCEPT_ALIASES.get(canonical, []))
    aliases.extend(OPTION_ONLY_CONCEPT_ALIASES.get(canonical, []))
    option_blob = _normalized_text(" ".join(_option_texts(q)))
    return any(alias in option_blob for alias in aliases)


def _stem_implies_described_concept(question_text: str) -> str | None:
    text = _normalized_text(question_text)
    best_match = None
    best_score = 0

    for canonical, config in DESCRIBED_CONCEPT_HINTS.items():
        groups = config.get("groups", [])
        threshold = int(config.get("threshold", 2) or 2)
        score = 0
        for group in groups:
            if any(phrase in text for phrase in group):
                score += 1
        if score >= threshold and score > best_score:
            best_match = canonical
            best_score = score

    return best_match


def _high_difficulty_uses_generic_benefit_stem(q: dict) -> bool:
    question_text = _normalized_text(str(q.get("question", "")))
    if not question_text:
        return False

    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4:
        return False

    if _has_high_difficulty_reasoning_layer(question_text):
        return False

    return any(marker in question_text for marker in _generic_benefit_stem_markers())


def _is_broad_positive_option(option_text: str) -> bool:
    text = _normalized_text(option_text)
    if not text:
        return False

    if any(negation in text for negation in ("not ", "avoid ", "prevent ", "reduce risk", "less ")):
        return False

    if len(text.split()) > 9:
        return False

    return any(marker in text for marker in _broad_positive_option_markers())


def _high_difficulty_has_ambiguous_multi_positive_options(q: dict) -> bool:
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4:
        return False

    question_text = _normalized_text(str(q.get("question", "")))
    if not question_text or not any(marker in question_text for marker in _generic_benefit_stem_markers()):
        return False

    positive_options = [text for text in _option_texts(q) if _is_broad_positive_option(text)]
    return len(positive_options) >= 2


def _high_difficulty_is_slogan_recall(q: dict) -> bool:
    question_text = _normalized_text(str(q.get("question", "")))
    if not question_text:
        return False

    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if difficulty < 4:
        return False

    if _has_high_difficulty_reasoning_layer(question_text):
        return False

    if any(marker in question_text for marker in _slogan_recall_stem_markers()):
        return True

    return (
        _looks_like_plain_recall_question(question_text)
        and any(
            marker in question_text
            for marker in ("principle", "pattern", "benefit", "goal", "characteristic", "purpose")
        )
    )


def _stem_option_mismatch_reason(q: dict) -> str | None:
    question_text = str(q.get("question", ""))
    if not question_text:
        return None

    if not _stem_asks_for_named_concept_choice(question_text):
        return None

    named_concept = _stem_implies_named_concept(question_text)
    if named_concept and not _options_include_named_concept(q, named_concept):
        return "stem_option_mismatch_named_concept"

    described_concept = _stem_implies_described_concept(question_text)
    if described_concept and not _options_include_named_concept(q, described_concept):
        return "stem_option_mismatch_described_concept"

    return None


def _concept_rule_matches(text: str, phrases: list[str]) -> bool:
    if not phrases:
        return False

    if len(phrases) == 1:
        return _text_contains_any(text, phrases)

    if len(phrases) == 2:
        return all(phrase in text for phrase in phrases)

    return _count_text_phrase_matches(text, phrases) >= 2


def _question_family_from_git_workflow(text: str) -> str | None:
    if _count_text_phrase_matches(
        text,
        [
            "distributed version control",
            "full repository",
            "local history",
            "full copy of history",
            "full local copy",
            "local repository",
            "local copy",
            "offline",
            "work offline",
            "later sync",
            "later synchronize",
            "each developer has a full repository",
        ],
    ) >= 2:
        return "distributed_vcs"
    if _count_text_phrase_matches(text, ["push rejected", "remote is ahead", "rejected because", "pull before pushing"]) >= 2:
        return "remote_ahead_push_rejected"
    if _count_text_phrase_matches(text, ["merge conflict", "overlapping edits", "resolve", "conflict resolution", "same lines"]) >= 2:
        return "merge_conflict_resolution"
    if _count_text_phrase_matches(text, ["two parent", "two previous commits", "merged histories", "points to both", "points to c3 and c4"]) >= 1:
        return "merge_commit_graph"
    if _count_text_phrase_matches(text, ["continuous delivery", "continuous deployment"]) >= 2:
        return "delivery_vs_deployment"
    if _count_text_phrase_matches(text, ["continuous integration", "after commit", "after pushing", "build", "test", "commit triggers"]) >= 2:
        return "ci_build_per_commit"
    if _count_text_phrase_matches(
        text,
        [
            "commit message",
            "meaningful",
            "descriptive",
            "clear summary",
            "good message",
            "specific",
            "vague",
            "what changed",
            "why",
            "history",
            "review",
        ],
    ) >= 2:
        return "commit_message_quality"
    if _count_text_phrase_matches(text, ["local repository", "remote repository", "local repo", "remote repo", "working copy", "shared repository"]) >= 2:
        return "local_vs_remote_repo"
    if _count_text_phrase_matches(text, ["isolated", "feature branch", "without affecting", "experimental change", "radical changes"]) >= 2:
        return "branch_isolation"
    if _count_text_phrase_matches(text, ["branch", "trunk", "mainline", "main branch"]) >= 2:
        return "branch_vs_trunk"
    if _count_text_phrase_matches(text, ["committed locally", "local commit", "teammates still", "still do not see", "push"]) >= 2:
        return "commit_vs_push"
    if _count_text_phrase_matches(text, ["already has a local", "already has the repository", "teammate", "pushed", "remote is ahead", "pull", "latest changes"]) >= 2:
        return "pull_remote_updates"
    if _count_text_phrase_matches(text, ["join the team", "first time", "first day", "local copy", "clone"]) >= 2:
        return "clone_first_checkout"
    if _count_text_phrase_matches(text, ["sequence", "order", "what happens next", "next step", "workflow"]) >= 2:
        return "workflow_sequence"
    return None


def _concept_focus_from_git_workflow(text: str) -> str | None:
    if _count_text_phrase_matches(text, ["full local copy", "full repository", "repository copy", "local copy", "local repository"]) >= 1:
        return "full_local_copy"
    if _count_text_phrase_matches(text, ["local history", "full copy of history", "repository history"]) >= 1:
        return "local_history"
    if _count_text_phrase_matches(text, ["offline work", "offline", "work offline", "without constant remote access"]) >= 1:
        return "offline_work"
    if _count_text_phrase_matches(
        text,
        [
            "distributed version control",
            "full repository",
            "local history",
            "full copy of history",
            "full local copy",
            "local repository",
            "local copy",
            "offline",
            "work offline",
            "later sync",
            "later synchronize",
            "each developer has a full repository",
        ],
    ) >= 2:
        return "distributed_vcs"
    if _count_text_phrase_matches(text, ["continuous delivery", "continuous deployment"]) >= 2:
        return "delivery_vs_deployment"
    if _count_text_phrase_matches(text, ["continuous integration", "after commit", "after pushing", "build", "test", "commit triggers"]) >= 2:
        return "ci_build_per_commit"
    if _count_text_phrase_matches(text, ["commit message", "meaningful", "descriptive", "clear summary", "good message"]) >= 2:
        return "commit_message_quality"
    if _count_text_phrase_matches(text, ["merge conflict", "overlapping edits", "resolve", "conflict resolution", "same lines"]) >= 2:
        return "merge_conflict_resolution"
    if _count_text_phrase_matches(text, ["two parent", "two previous commits", "merged histories", "points to both", "points to c3 and c4"]) >= 1:
        return "merge_commit_graph"
    if _count_text_phrase_matches(text, ["local repository", "remote repository", "local repo", "remote repo", "working copy", "shared repository"]) >= 2:
        return "local_vs_remote_repo"
    if _count_text_phrase_matches(text, ["isolated", "feature branch", "without affecting", "experimental change", "radical changes"]) >= 2:
        return "branch_isolation"
    if _count_text_phrase_matches(text, ["branch", "trunk", "mainline", "main branch"]) >= 2:
        return "branch_vs_trunk"
    if _count_text_phrase_matches(text, ["committed locally", "local commit", "teammates still", "still do not see", "push"]) >= 2:
        return "commit_vs_push"
    if _count_text_phrase_matches(text, ["already has a local", "already has the repository", "teammate", "pushed", "remote is ahead", "pull", "latest changes"]) >= 2:
        return "pull_remote_updates"
    if _count_text_phrase_matches(text, ["join the team", "first time", "first day", "local copy", "clone"]) >= 2:
        return "clone_first_checkout"
    return None


def _concept_focus_from_ui_ux(text: str) -> str | None:
    if _count_text_phrase_matches(text, ["aggregate related questions", "single dialog", "single dialogue", "many related questions"]) >= 2:
        return "aggregate_questions_single_dialog"
    if _count_text_phrase_matches(text, ["text label", "icons alone", "screen reader", "accessible label"]) >= 2:
        return "accessibility_text_labels"
    if _count_text_phrase_matches(text, ["recognition rather than recall", "memory load", "reduce memory load"]) >= 2:
        return "reduce_memory_load"
    if _count_text_phrase_matches(text, ["reversible action", "confirmation dialog", "undo instead", "confirmation before"]) >= 2:
        return "reversible_actions_vs_confirmation"
    if _count_text_phrase_matches(text, ["visible system state", "visible state", "show current state", "status visibility"]) >= 2:
        return "visible_state"
    if _count_text_phrase_matches(text, ["visible feedback", "feedback after action", "acknowledgment after action"]) >= 2:
        return "visible_feedback"
    if "horizontal prototype" in text:
        return "horizontal_prototype"
    if "vertical prototype" in text:
        return "vertical_prototype"
    if "t prototype" in text or "t-shaped prototype" in text or "t shaped prototype" in text:
        return "t_prototype"
    if "local prototype" in text:
        return "local_prototype"
    if _count_text_phrase_matches(text, ["low fidelity", "paper prototype", "iterative", "rough prototype"]) >= 2:
        return "low_fidelity_iterative_design"
    if _count_text_phrase_matches(text, ["involve users early", "real users early", "user feedback early", "users early in design"]) >= 2:
        return "early_user_involvement"
    return None


def _concept_focus_from_testing(text: str) -> str | None:
    if _count_text_phrase_matches(text, ["static verification", "dynamic verification"]) >= 2:
        return "static_vs_dynamic_verification"
    if _count_text_phrase_matches(text, ["test suite", "same coverage", "fewer test cases", "efficiency"]) >= 2:
        return "test_suite_efficiency"
    if _count_text_phrase_matches(text, ["white box", "internal structure", "source code knowledge"]) >= 2:
        return "white_box_code_knowledge"
    if _count_text_phrase_matches(text, ["most likely to find", "find the most errors", "find more defects"]) >= 2:
        return "most_likely_to_find_errors"
    if _count_text_phrase_matches(text, ["equivalence partition", "equivalence class"]) >= 1:
        return "equivalence_partitioning"
    if _count_text_phrase_matches(text, ["independent tester", "programmer tests", "separate tester"]) >= 2:
        return "programmer_vs_independent_tester"
    if _count_text_phrase_matches(text, ["static analyzer", "static analyzers", "static analysis tool"]) >= 1:
        return "static_analyzers"
    if _count_text_phrase_matches(text, ["inspection", "inspections", "formal review"]) >= 1:
        return "inspections"
    if "dynamic verification" in text:
        return "dynamic_verification"
    if "static verification" in text:
        return "static_verification"
    if _count_text_phrase_matches(text, ["goal of testing", "find errors", "find defects"]) >= 2:
        return "goal_of_testing"
    return None


def _concept_focus_from_design(text: str) -> str | None:
    if _count_text_phrase_matches(text, ["linked list", "stack", "wrapper", "without modifying"]) >= 2:
        return "adapter_wrapper_stack"
    if _count_text_phrase_matches(text, ["wrapper", "modify the existing class", "without modifying", "existing class"]) >= 2:
        return "wrapper_vs_modify_existing_class"
    if "adapter pattern" in text or ("adapter" in text and "wrapper" in text):
        return "adapter_pattern"
    if _count_text_phrase_matches(text, ["high cohesion", "cohesion"]) >= 1:
        return "high_cohesion"
    if _count_text_phrase_matches(text, ["low coupling", "loosely coupled", "reduce dependencies"]) >= 1:
        return "low_coupling"
    return None


def derive_question_family(question: dict, course_id: str | None = None) -> str | None:
    if not isinstance(question, dict):
        return None

    existing = str(question.get("question_family", "")).strip().lower()
    if existing:
        return existing

    text_blob = " ".join([
        str(question.get("topic", "")),
        str(question.get("question", "") or question.get("question_text", "")),
        " ".join(str(v) for v in (question.get("options", {}) or {}).values()),
        str(question.get("explanation", "")),
    ])
    text = _normalized_text(text_blob)
    if not text:
        return None

    if is_csen603_git_workflow_scope(course_id, str(question.get("topic", "")), text_blob):
        return _question_family_from_git_workflow(text)

    return None


def derive_concept_focus(question: dict) -> str | None:
    if not isinstance(question, dict):
        return None

    existing = str(question.get("concept_focus", "")).strip().lower()
    if existing:
        return existing

    text_blob = " ".join([
        str(question.get("topic", "")),
        str(question.get("question", "") or question.get("question_text", "")),
        " ".join(str(v) for v in (question.get("options", {}) or {}).values()),
        str(question.get("explanation", "")),
    ])
    text = _normalized_text(text_blob)
    if not text:
        return None

    for resolver in (
        _concept_focus_from_git_workflow,
        _concept_focus_from_ui_ux,
        _concept_focus_from_testing,
        _concept_focus_from_design,
    ):
        concept_focus = resolver(text)
        if concept_focus:
            return concept_focus

    for canonical, aliases in NAMED_CONCEPT_ALIASES.items():
        option_aliases = OPTION_ONLY_CONCEPT_ALIASES.get(canonical, [])
        if any(alias in text for alias in list(aliases) + list(option_aliases)):
            return CONCEPT_FOCUS_TOKEN_MAP.get(canonical, canonical)

    for canonical, config in DESCRIBED_CONCEPT_HINTS.items():
        groups = config.get("groups", [])
        threshold = int(config.get("threshold", 2) or 2)
        score = 0
        for group in groups:
            if any(phrase in text for phrase in group):
                score += 1
        if score >= threshold:
            return CONCEPT_FOCUS_TOKEN_MAP.get(canonical, canonical)

    for token, phrases in CONCEPT_FOCUS_RULES:
        if _concept_rule_matches(text, phrases):
            return token

    if "abstraction" in text:
        return "abstraction"
    if "information hiding" in text or "encapsulation" in text:
        return "information_hiding"

    return None


def ensure_question_concept_focus(question: dict, course_id: str | None = None) -> dict:
    if not isinstance(question, dict):
        return question

    concept_focus = derive_concept_focus(question)
    if concept_focus:
        question["concept_focus"] = concept_focus

    question_family = derive_question_family(question, course_id or question.get("course_id"))
    if question_family:
        question["question_family"] = question_family
    return question


def _git_topic_bucket(topic: str) -> str | None:
    text = _normalized_text(topic)
    if not text:
        return None

    if _text_contains_any(text, ["local vs remote", "local remote", "remote repository", "working copy"]):
        return "local_vs_remote"
    if _text_contains_any(text, ["merge conflict", "merge conflicts"]):
        return "merge_conflicts"
    if _text_contains_any(text, ["branching and merging", "branching", "merging", "branch", "mainline", "trunk"]):
        return "branching_and_merging"
    if _text_contains_any(text, ["commit quality", "commit message", "commit messages"]):
        return "commit_quality"
    if _text_contains_any(text, ["distributed version control", "version control"]):
        return "distributed_vcs"
    if _text_contains_any(text, ["ci and cd", "continuous integration", "continuous delivery", "continuous deployment", "ci/cd"]):
        return "ci_cd_fundamentals"
    if _text_contains_any(text, ["git workflow basics", "git workflow", "git basics"]):
        return "git_workflow_basics"
    return None


def _is_fragile_git_topic_bucket(topic_bucket: str | None) -> bool:
    return topic_bucket in GIT_FRAGILE_TOPIC_BUCKETS


def _git_safe_vocabulary_terms(
    q: dict,
    source_text: str | None,
    *,
    course_id: str | None = None,
) -> set[str]:
    if not is_csen603_git_workflow_scope(course_id, str(q.get("topic", "")), source_text):
        return set()

    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    safe_phrases: dict[str, tuple[str, ...]] = {
        "branching_and_merging": (
            "branch",
            "feature branch",
            "mainline",
            "trunk",
            "merge",
            "merge commit",
            "conflict",
            "reconcile",
            "integrate",
            "parallel development",
            "local branch",
            "main branch",
        ),
        "merge_conflicts": (
            "conflict",
            "conflicting",
            "resolve",
            "resolved",
            "manual",
            "manually",
            "files",
            "edit",
            "edited",
            "combine",
            "reconcile",
        ),
        "commit_quality": (
            "clear",
            "descriptive",
            "context",
            "message",
            "useful",
            "meaningful",
            "specific",
            "specificity",
            "vague",
            "clarity",
            "review",
            "reviewers",
            "teammates",
            "history",
            "what changed",
            "why it changed",
        ),
        "distributed_vcs": (
            "offline",
            "local copy",
            "local history",
            "full copy",
            "synchronize",
            "synchronization",
            "later sync",
            "work offline",
            "collaboration",
            "local repository",
            "repository history",
            "distributed copy",
            "repository copy",
        ),
        "ci_cd_fundamentals": (
            "build",
            "test",
            "pipeline",
            "integration machine",
            "release-ready",
            "deploy",
            "deployment",
            "delivery",
        ),
    }
    phrases = safe_phrases.get(topic_bucket, ())
    safe_terms: set[str] = set()
    for phrase in phrases:
        safe_terms.update(_significant_terms(phrase))
    return safe_terms


def _git_correct_option_text(q: dict) -> str:
    options = q.get("options", {}) or {}
    correct_letter = q.get("correct_answer")
    return _normalized_text(str(options.get(correct_letter, "")))


def _git_question_text(q: dict) -> str:
    return _normalized_text(str(q.get("question", "")))


def _git_text_blob(q: dict) -> str:
    return _normalized_text(
        " ".join(
            [
                str(q.get("topic", "")),
                str(q.get("question", "")),
                " ".join(str(value) for value in (q.get("options", {}) or {}).values()),
                str(q.get("explanation", "")),
            ]
        )
    )


def _git_family(q: dict, course_id: str | None) -> str | None:
    return derive_question_family(q, course_id)


def _text_has_push_visibility_signal(text: str) -> bool:
    return _count_text_phrase_matches(
        text,
        [
            "teammate does not see",
            "teammates do not see",
            "visible to teammates",
            "share the change",
            "available to teammates",
            "visible to the team",
        ],
    ) >= 1


def _text_has_local_update_signal(text: str) -> bool:
    return _count_text_phrase_matches(
        text,
        [
            "already has a local",
            "already has the repository",
            "latest changes",
            "teammate pushed",
            "update local",
            "update the local repository",
        ],
    ) >= 1


def _question_sentence_count(text: str) -> int:
    stripped = str(text or "").strip()
    if not stripped:
        return 0
    parts = [part for part in re.split(r"(?<=[.!?])\s+", stripped) if part.strip()]
    return len(parts) or 1


def _git_commit_quality_focus_score(text: str) -> int:
    groups = [
        ["commit message", "message"],
        ["specific", "specificity", "descriptive", "meaningful", "clear", "vague"],
        ["what changed", "scope", "changed"],
        ["why", "reason", "context", "purpose"],
        ["history", "review", "reviewer", "teammate", "maintenance", "future understanding"],
    ]
    return sum(1 for group in groups if _text_contains_any(text, group))


def _git_commit_quality_has_workflow_drift(text: str) -> bool:
    return _text_contains_any(
        text,
        [
            "push",
            "pull",
            "merge conflict",
            "feature branch",
            "branch isolation",
            "continuous integration",
            "continuous delivery",
            "continuous deployment",
            "pipeline",
            "pull request",
            "github",
            "git commit --amend",
            "amend",
            "recommit",
            "rebase",
            "cherry-pick",
            "cherry pick",
        ],
    )


def _git_commit_quality_template_match(question_text: str, question_blob: str) -> bool:
    if _git_commit_quality_has_workflow_drift(question_blob):
        return False
    if not _text_contains_any(question_blob, ["commit message", "message"]):
        return False
    return _git_commit_quality_focus_score(question_blob) >= 2


def _git_branching_has_advanced_drift(text: str) -> bool:
    return _text_contains_any(
        text,
        [
            "cherry-pick",
            "cherry pick",
            "revert",
            "rename file",
            "renamed file",
            "workaround",
            "silent bug",
            "critical shared config",
            "team lead",
            "deadline",
            "competing priorities",
        ],
    )


def _git_branching_stem_is_overelaborate(question_text: str) -> bool:
    return _question_sentence_count(question_text) > 2 or _count_text_phrase_matches(
        question_text,
        [
            "while",
            "without",
            "before",
            "after",
            "at the same time",
            "must also",
            "simultaneously",
        ],
    ) >= 3


def _git_dvc_reserve_focus_ok(q: dict) -> bool:
    concept_focus = derive_concept_focus(q) or ""
    return concept_focus in {"distributed_vcs", "full_local_copy", "local_history", "offline_work"}


def _git_challenge_safe_blocks_reserve_candidate(q: dict, reasons: list[str], course_id: str | None) -> bool:
    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    question_text = _git_question_text(q)
    question_blob = _git_text_blob(q)
    question_family = _git_family(q, course_id)

    if any(
        reason in reasons
        for reason in (
            "git_ci_cd_topic_drift",
            "git_commit_quality_wrong_focus",
            "git_topic_family_misalignment",
            "git_branching_stem_too_elaborate",
            "git_merge_conflict_logic_failure",
            "git_visibility_push_vs_pull_confusion",
        )
    ):
        return True

    if "too_many_out_of_source_specifics" in reasons and topic_bucket in {
        "distributed_vcs",
        "commit_quality",
        "ci_cd_fundamentals",
        "merge_conflicts",
        "branching_and_merging",
    }:
        return True

    if topic_bucket == "branching_and_merging":
        return _git_branching_stem_is_overelaborate(question_text) or _git_branching_has_advanced_drift(question_blob)

    if topic_bucket == "ci_cd_fundamentals":
        if question_family not in {"ci_build_per_commit", "delivery_vs_deployment"}:
            return True
        return any(
            reason in reasons
            for reason in (
                "generic_benefit_stem_high_difficulty",
                "slogan_recall_high_difficulty",
                "plain_recall_high_difficulty",
                "too_direct_high_difficulty",
            )
        )

    if topic_bucket == "commit_quality":
        return (
            question_family != "commit_message_quality"
            or not _git_commit_quality_template_match(question_text, question_blob)
        )

    if topic_bucket == "distributed_vcs":
        return not _git_dvc_reserve_focus_ok(q)

    return False


def _git_topic_focus_markers(topic_bucket: str | None) -> tuple[str, ...]:
    marker_map: dict[str | None, tuple[str, ...]] = {
        "branching_and_merging": (
            "branch",
            "feature branch",
            "trunk",
            "mainline",
            "main branch",
            "merge",
            "merge commit",
            "parallel development",
        ),
        "merge_conflicts": ("merge conflict", "conflicting", "resolve", "reconcile", "overlapping edits"),
        "commit_quality": ("commit message", "descriptive", "specific", "meaningful", "what changed", "why"),
        "distributed_vcs": ("distributed version control", "full local copy", "local history", "full repository", "offline"),
        "ci_cd_fundamentals": ("continuous integration", "continuous delivery", "continuous deployment", "build", "test", "pipeline", "integration machine"),
    }
    return marker_map.get(topic_bucket, ())


def _git_action_family(option_text: str) -> str | None:
    text = _normalized_text(option_text)
    if not text:
        return None
    if "clone" in text:
        return "clone"
    if "push" in text:
        return "push"
    if "pull" in text or ("fetch" in text and any(token in text for token in ("merge", "rebase", "synchronize"))):
        return "sync"
    if any(token in text for token in ("feature branch", "separate branch", "create a branch", "branch")):
        return "branch"
    if any(token in text for token in ("resolve", "reconcile", "edit the conflicting", "fix the conflicting", "merge the changes")):
        return "resolve_conflict"
    if any(token in text for token in ("commit message", "descriptive", "specific", "meaningful", "what changed", "why")):
        return "commit_quality"
    if any(token in text for token in ("continuous integration", "continuous delivery", "continuous deployment", "build", "test", "pipeline")):
        return "ci_cd"
    if any(token in text for token in ("full repository", "local history", "full local copy", "offline", "distributed version control")):
        return "distributed_vcs"
    return None


def _git_dual_correct_operational_options(q: dict, course_id: str | None) -> bool:
    question_text = _git_question_text(q)
    question_family = _git_family(q, course_id)
    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    if not question_text or not any(
        marker in question_text
        for marker in ("what should", "best action", "which action", "which command", "what should happen next", "workflow mistake")
    ):
        return False

    accepted_by_family: dict[str | None, set[str]] = {
        "commit_vs_push": {"push"},
        "pull_remote_updates": {"sync"},
        "branch_isolation": {"branch"},
        "remote_ahead_push_rejected": {"sync"},
        "merge_conflict_resolution": {"resolve_conflict"},
        "commit_message_quality": {"commit_quality"},
        "ci_build_per_commit": {"ci_cd"},
        "delivery_vs_deployment": {"ci_cd"},
        "distributed_vcs": {"distributed_vcs"},
    }
    accepted_actions = accepted_by_family.get(question_family)
    if not accepted_actions and topic_bucket == "ci_cd_fundamentals":
        accepted_actions = {"ci_cd"}
    if not accepted_actions and topic_bucket == "commit_quality":
        accepted_actions = {"commit_quality"}
    if not accepted_actions:
        return False

    matching_options = 0
    for option_text in _option_texts(q):
        option_action = _git_action_family(option_text)
        if option_action in accepted_actions:
            matching_options += 1
    return matching_options >= 2


def _git_fragile_topic_reserve_drift(q: dict, course_id: str | None) -> bool:
    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    if not _is_fragile_git_topic_bucket(topic_bucket):
        return False

    text_blob = _git_text_blob(q)
    focus_markers = _git_topic_focus_markers(topic_bucket)
    if focus_markers and not any(marker in text_blob for marker in focus_markers):
        return True

    if topic_bucket == "ci_cd_fundamentals":
        return _git_family(q, course_id) not in {"ci_build_per_commit", "delivery_vs_deployment", "workflow_sequence"}
    if topic_bucket == "commit_quality":
        return not _git_commit_quality_template_match(_git_question_text(q), text_blob)
    if topic_bucket == "distributed_vcs":
        return not _git_dvc_reserve_focus_ok(q)
    if topic_bucket == "merge_conflicts":
        return _git_family(q, course_id) not in {"merge_conflict_resolution", "remote_ahead_push_rejected", "merge_commit_graph"}
    return False


def _git_topic_family_alignment_reason(q: dict, course_id: str | None) -> str | None:
    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    question_family = _git_family(q, course_id)
    question_text = _git_question_text(q)
    question_blob = _git_text_blob(q)
    if not topic_bucket or not question_family:
        return None

    allowed: dict[str, set[str]] = {
        "local_vs_remote": {
            "clone_first_checkout",
            "pull_remote_updates",
            "commit_vs_push",
            "local_vs_remote_repo",
            "remote_ahead_push_rejected",
            "workflow_sequence",
        },
        "merge_conflicts": {
            "merge_conflict_resolution",
            "remote_ahead_push_rejected",
            "merge_commit_graph",
        },
        "branching_and_merging": {
            "branch_isolation",
            "branch_vs_trunk",
            "merge_commit_graph",
            "merge_conflict_resolution",
        },
        "commit_quality": {
            "commit_message_quality",
        },
        "distributed_vcs": {
            "distributed_vcs",
            "local_vs_remote_repo",
            "clone_first_checkout",
        },
        "git_workflow_basics": {
            "clone_first_checkout",
            "pull_remote_updates",
            "commit_vs_push",
            "local_vs_remote_repo",
            "workflow_sequence",
            "remote_ahead_push_rejected",
            "branch_isolation",
        },
        "ci_cd_fundamentals": {
            "ci_build_per_commit",
            "delivery_vs_deployment",
            "workflow_sequence",
        },
    }
    soft_conditions = {
        ("local_vs_remote", "workflow_sequence"): _text_contains_any(
            question_blob,
            [
                "clone",
                "pull",
                "push",
                "commit",
                "local repository",
                "remote repository",
                "new local copy",
                "existing local copy",
                "latest remote changes",
                "already has a local",
            ],
        ),
        ("branching_and_merging", "merge_conflict_resolution"): _text_contains_any(
            question_blob,
            ["merge conflict", "conflicting", "resolve", "reconcile"],
        ) and not _git_branching_has_advanced_drift(question_blob) and _question_sentence_count(question_text) <= 2,
        ("merge_conflicts", "merge_commit_graph"): _text_contains_any(
            question_blob,
            ["two parent", "two previous commits", "merged histories", "merge history", "points to both"],
        ),
        ("distributed_vcs", "workflow_sequence"): _text_contains_any(
            question_text,
            ["distributed", "repository", "local copy", "local history", "offline", "full copy"],
        ),
        ("ci_cd_fundamentals", "workflow_sequence"): _text_contains_any(
            question_text,
            ["continuous integration", "continuous delivery", "continuous deployment", "build", "test", "pipeline"],
        ),
        ("commit_quality", "commit_message_quality"): _git_commit_quality_template_match(question_text, question_blob),
        ("distributed_vcs", "local_vs_remote_repo"): _text_contains_any(
            question_blob,
            ["full local copy", "full repository", "local history", "local repository", "offline", "later sync", "later synchronize"],
        ),
        ("distributed_vcs", "clone_first_checkout"): _text_contains_any(
            question_blob,
            ["full local copy", "full repository", "local history", "local repository", "offline", "work offline"],
        ),
    }

    if question_family in allowed.get(topic_bucket, set()):
        if topic_bucket == "branching_and_merging" and question_family == "merge_conflict_resolution":
            return None if soft_conditions[("branching_and_merging", "merge_conflict_resolution")] else "git_topic_family_misalignment"
        if topic_bucket == "merge_conflicts" and question_family == "merge_commit_graph":
            return None if soft_conditions[("merge_conflicts", "merge_commit_graph")] else "git_topic_family_misalignment"
        if topic_bucket == "ci_cd_fundamentals" and question_family == "workflow_sequence":
            return None if soft_conditions[("ci_cd_fundamentals", "workflow_sequence")] else "git_ci_cd_topic_drift"
        if topic_bucket == "commit_quality" and question_family == "commit_message_quality":
            return None if soft_conditions[("commit_quality", "commit_message_quality")] else "git_commit_quality_wrong_focus"
        if topic_bucket == "distributed_vcs" and question_family == "local_vs_remote_repo":
            return None if soft_conditions[("distributed_vcs", "local_vs_remote_repo")] else "git_topic_family_misalignment"
        if topic_bucket == "distributed_vcs" and question_family == "clone_first_checkout":
            return None if soft_conditions[("distributed_vcs", "clone_first_checkout")] else "git_topic_family_misalignment"
        return None

    if (topic_bucket, question_family) in soft_conditions and soft_conditions[(topic_bucket, question_family)]:
        return None

    if topic_bucket == "ci_cd_fundamentals":
        return "git_ci_cd_topic_drift"
    if topic_bucket == "commit_quality":
        return "git_commit_quality_wrong_focus"
    return "git_topic_family_misalignment"


def _git_correctness_reasons(q: dict, source_text: str | None, course_id: str | None) -> list[str]:
    if not is_csen603_git_workflow_scope(course_id, str(q.get("topic", "")), source_text):
        return []

    question_text = _git_question_text(q)
    question_blob = _git_text_blob(q)
    correct_text = _git_correct_option_text(q)
    question_family = _git_family(q, course_id)
    topic_bucket = _git_topic_bucket(str(q.get("topic", "")))
    concept_focus = derive_concept_focus(q) or ""
    reasons: list[str] = []
    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    if (
        question_family == "commit_vs_push"
        or _text_has_push_visibility_signal(question_text)
    ):
        if "push" not in correct_text or any(token in correct_text for token in ("pull", "clone")):
            reasons.append("git_visibility_push_vs_pull_confusion")

    if (
        question_family == "pull_remote_updates"
        or _text_has_local_update_signal(question_text)
    ):
        allows_update = (
            "pull" in correct_text
            or ("fetch" in correct_text and any(token in correct_text for token in ("merge", "rebase", "synchronize")))
        )
        if not allows_update or any(token in correct_text for token in ("clone", "push")):
            reasons.append("git_local_update_wrong_action")

    if question_family == "branch_isolation":
        if not any(token in correct_text for token in ("branch", "feature branch")) or any(token in correct_text for token in ("trunk", "mainline", "main branch", "directly on main")):
            reasons.append("git_branch_isolation_wrong_action")

    if "merge conflict" in question_text or question_family == "merge_conflict_resolution":
        if not any(token in correct_text for token in ("resolve", "reconcile", "edit the conflicting", "fix the conflicting", "merge the changes")):
            reasons.append("git_merge_conflict_logic_failure")
        elif any(token in correct_text for token in ("clone", "start a new branch")):
            reasons.append("git_merge_conflict_logic_failure")
        if any(
            marker in question_blob
            for marker in (
                "push causes a merge conflict",
                "pushing causes a merge conflict",
                "push directly creates a merge conflict",
                "merge conflict at push time",
                "overwritten automatically",
                "lost forever",
                "overwrite the remote history",
            )
        ):
            reasons.append("git_merge_conflict_wording_failure")

    if question_family == "remote_ahead_push_rejected" or _text_contains_any(question_text, ["remote is ahead", "push rejected", "another developer pushed first"]):
        if not any(token in correct_text for token in ("pull", "synchronize", "merge", "reconcile", "fetch")) or "push directly" in correct_text:
            reasons.append("git_remote_ahead_sync_failure")

    if topic_bucket == "branching_and_merging":
        if difficulty >= 4 and (
            _git_branching_stem_is_overelaborate(question_text)
            or _git_branching_has_advanced_drift(question_blob)
        ):
            reasons.append("git_branching_stem_too_elaborate")

    if topic_bucket == "commit_quality":
        if not _git_commit_quality_template_match(question_text, question_blob):
            reasons.append("git_commit_quality_wrong_focus")
        elif not any(token in correct_text for token in ("specific", "descriptive", "meaningful", "what changed", "why", "context", "history", "review")):
            reasons.append("git_commit_quality_wrong_focus")
        if any(token in correct_text for token in ("recommit", "git commit --amend", "ignore the message")):
            reasons.append("git_commit_quality_wrong_focus")

    if topic_bucket == "ci_cd_fundamentals" and not any(marker in question_blob for marker in _git_topic_focus_markers(topic_bucket)):
        reasons.append("git_ci_cd_topic_drift")

    if topic_bucket == "distributed_vcs" and (
        not any(marker in question_blob for marker in _git_topic_focus_markers(topic_bucket))
        or concept_focus not in {"distributed_vcs", "full_local_copy", "local_history", "offline_work"}
    ):
        reasons.append("git_topic_family_misalignment")

    if topic_bucket == "merge_conflicts" and not any(marker in question_blob for marker in _git_topic_focus_markers(topic_bucket)):
        reasons.append("git_merge_conflict_logic_failure")

    alignment_reason = _git_topic_family_alignment_reason(q, course_id)
    if alignment_reason:
        reasons.append(alignment_reason)

    if _git_dual_correct_operational_options(q, course_id):
        reasons.append("git_dual_correct_operational_options")

    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def _difficulty_mismatch_reasons(
    q: dict,
    source_text: str | None = None,
    *,
    validation_profile: str = "default",
    course_id: str | None = None,
) -> list[str]:
    question_text = str(q.get("question", ""))
    reasons = []

    try:
        difficulty = int(q.get("difficulty", 3))
    except Exception:
        difficulty = 3

    stem_option_mismatch_reason = _stem_option_mismatch_reason(q)
    if stem_option_mismatch_reason:
        reasons.append(stem_option_mismatch_reason)

    if difficulty >= 4 and _looks_like_plain_recall_question(question_text):
        reasons.append("plain_recall_high_difficulty")

    if difficulty >= 4 and _high_difficulty_looks_too_direct(q):
        reasons.append("too_direct_high_difficulty")

    if difficulty >= 4 and _high_difficulty_uses_generic_benefit_stem(q):
        reasons.append("generic_benefit_stem_high_difficulty")

    if difficulty >= 4 and _high_difficulty_has_ambiguous_multi_positive_options(q):
        reasons.append("ambiguous_multi_positive_options_high_difficulty")

    if difficulty >= 4 and _high_difficulty_is_slogan_recall(q):
        reasons.append("slogan_recall_high_difficulty")

    if difficulty >= 4 and _high_difficulty_distractors_look_too_weak(q):
        if validation_profile == "diagnostic" and _diagnostic_allows_borderline_distractors(q):
            logger.info(
                "[DIAG] Diagnostic-specific leniency applied validator=%s topic=%s",
                "weak_distractors_high_difficulty",
                q.get("topic"),
            )
        else:
            reasons.append("weak_distractors_high_difficulty")

    if source_text and difficulty >= 4 and _introduces_unsupported_numeric_detail(q, source_text):
        reasons.append("unsupported_numeric_detail")

    if source_text and difficulty >= 4 and _introduces_too_many_out_of_source_specifics(
        q,
        source_text,
        validation_profile=validation_profile,
        course_id=course_id,
    ):
        reasons.append("too_many_out_of_source_specifics")

    if source_text and difficulty >= 4 and _high_difficulty_is_too_close_to_source(q, source_text):
        reasons.append("too_close_to_source")

    if difficulty <= 2 and _looks_like_reasoning_question(question_text):
        reasons.append("too_reasoning_heavy_low_difficulty")

    reasons.extend(_git_correctness_reasons(q, source_text, course_id))

    return reasons


async def _generate_question_once(
    topic: str,
    difficulty: int,
    source_text: str,
    course_id: str | None = None,
    return_metadata: bool = False,
    max_provider_model_attempts: int | None = None,
    generation_profile: str = "default",
    allow_last_resort: bool = True,
) -> dict | tuple[dict, dict]:
    difficulty_label = {
    1: "very easy",
    2: "easy",
    3: "medium",
    4: "hard",
    5: "very hard",
    }[difficulty]
    variation, variation_bucket = _choose_variation_angle(difficulty)

    clean_source_text = _sanitize_source_text_for_questions(source_text) or source_text
    if generation_profile == "diagnostic":
        original_nonempty_lines = sum(1 for line in source_text.splitlines() if line.strip())
        cleaned_nonempty_lines = sum(1 for line in clean_source_text.splitlines() if line.strip())
        removed_lines = max(0, original_nonempty_lines - cleaned_nonempty_lines)
        if removed_lines:
            logger.info("[DIAG] Sanitized admin boilerplate topic=%s removed_lines=%s", topic, removed_lines)
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

    brittle_policy = _build_hard_short_scope_policy(
        difficulty=difficulty,
        context_mode=context_mode,
        selected_context=selected_context,
        selected_chunk_count=selected_chunk_count,
        max_provider_model_attempts=max_provider_model_attempts,
    )
    effective_attempt_budget = brittle_policy["effective_attempt_budget"]
    if brittle_policy["risky_generation"]:
        logger.info(
            "[LLM] Hard-short-scope risk detected topic=%s difficulty=%s selected_chars=%s context_mode=%s seed_chunks=%s max_attempts=%s",
            topic,
            difficulty,
            brittle_policy["selected_chars"],
            context_mode,
            selected_chunk_count,
            effective_attempt_budget,
        )

    course_style_block = _build_course_style_block(
        course_id,
        topic,
        difficulty,
        generation_profile,
        selected_context,
    )
    hard_quality_block = _build_hard_question_quality_block(
        course_id,
        topic,
        difficulty,
        generation_profile,
    )
    if course_style_block:
        logger.info(
            "[LLM] Course-specific prompt conditioning enabled course=%s topic=%s",
            course_id,
            topic,
        )

    prompt = PROMPT_TEMPLATE.format(
        variation=variation,
        topic=topic,
        difficulty_label=difficulty_label,
        difficulty=difficulty,
        course_style_block=course_style_block,
        hard_quality_block=hard_quality_block,
        source_text=selected_context
    )

    all_failures = []
    reserve_candidate = None
    provider_model_attempts = 0
    brittle_validation_failures = 0
    provider_failures = 0
    rate_limit_failures = 0

    def _metadata_payload(
        *,
        used_last_resort: bool,
        provider: str | None = None,
        model: str | None = None,
        validation_reasons_at_accept: list[str] | None = None,
    ) -> dict:
        payload = {
            "used_last_resort": used_last_resort,
            "validation_reasons_at_accept": list(validation_reasons_at_accept or []),
            "provider_model_attempts": provider_model_attempts,
            "budget_limited": effective_attempt_budget is not None,
            "max_provider_model_attempts": effective_attempt_budget,
            "hard_short_scope_risk": brittle_policy["risky_generation"],
            "selected_context_chars": brittle_policy["selected_chars"],
            "context_mode": brittle_policy["context_mode"],
            "seed_chunks": brittle_policy["selected_chunk_count"],
        }
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
            payload["source"] = f"{provider}/{model}" if provider else model
        return payload

    async with httpx.AsyncClient(timeout=30) as client:
        provider_order = _provider_order_for_generation(generation_profile)
        if generation_profile == "diagnostic":
            logger.info("[DIAG] Provider preference order active topic=%s order=%s", topic, provider_order)
        elif generation_profile == "git_challenge":
            logger.info("[LLM] Git challenge safe provider subset topic=%s order=%s", topic, provider_order)
        for provider_name in provider_order:
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

            models = _provider_models_for_generation(provider_name, generation_profile)
            if not models:
                logger.info(f"[LLM] Skipping provider={provider_name} (no models configured)")
                continue

            logger.info(f"[LLM] Trying provider={provider_name} models={models}")

            for model in models:
                if (
                    effective_attempt_budget is not None
                    and provider_model_attempts >= effective_attempt_budget
                ):
                    logger.info(
                        "[LLM] Attempt budget exhausted topic=%s difficulty=%s attempts=%s max_attempts=%s",
                        topic,
                        difficulty,
                        provider_model_attempts,
                        effective_attempt_budget,
                    )
                    break

                model_cooldown_remaining = _model_cooldown_remaining(provider_name, model)
                if model_cooldown_remaining > 0:
                    logger.info(
                        "[LLM] Skipping model cooldown provider=%s model=%s remaining=%ss",
                        provider_name,
                        model,
                        int(model_cooldown_remaining),
                    )
                    continue

                if generation_profile == "git_challenge":
                    unstable_remaining = _git_challenge_model_suppression_remaining(provider_name, model)
                    if unstable_remaining > 0:
                        logger.info(
                            "[LLM] Skipping unstable Git challenge model provider=%s model=%s remaining=%ss",
                            provider_name,
                            model,
                            int(unstable_remaining),
                        )
                        continue

                provider_model_attempts += 1
                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw)

                    q = json.loads(raw)
                    q = ensure_question_concept_focus(q, course_id=course_id)

                    validation_profile = "diagnostic" if generation_profile == "diagnostic" else "default"
                    is_valid, reasons = validate_question(
                        q,
                        selected_context,
                        validation_profile=validation_profile,
                        course_id=course_id,
                    )
                    if is_valid:
                        q = _shuffle_question_options(q)
                        logger.info(f"[LLM] SUCCESS provider={provider_name} model={model} topic={topic} difficulty={difficulty}")
                        if return_metadata:
                            return q, _metadata_payload(
                                used_last_resort=False,
                                provider=provider_name,
                                model=model,
                                validation_reasons_at_accept=[],
                            )
                        return q
                    else:
                        reason_text = ",".join(reasons) if reasons else "unknown"
                        msg = f"[LLM] Validation failed provider={provider_name} model={model} reasons={reason_text}"
                        logger.warning(msg)
                        all_failures.append(msg)
                        brittle_reasons = _brittle_reason_subset(reasons)
                        if brittle_policy["risky_generation"] and brittle_reasons:
                            brittle_validation_failures += 1
                            logger.info(
                                "[LLM] Hard-short-scope validation pressure topic=%s difficulty=%s count=%s reasons=%s",
                                topic,
                                difficulty,
                                brittle_validation_failures,
                                ",".join(brittle_reasons),
                            )

                        core_valid, _ = validate_question_core_only(q)
                        if (
                            core_valid
                            and reserve_candidate is None
                            and not _blocks_last_resort_accept_for_candidate(
                                q,
                                reasons,
                                source_text=selected_context,
                                course_id=course_id,
                            )
                            and not (
                                generation_profile == "git_challenge"
                                and _git_challenge_safe_blocks_reserve_candidate(q, reasons, course_id)
                            )
                        ):
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
                        elif core_valid and _blocks_last_resort_accept_for_candidate(
                            q,
                            reasons,
                            source_text=selected_context,
                            course_id=course_id,
                        ):
                            logger.info(
                                "[LLM] Reserve candidate skipped provider=%s model=%s blocking_reasons=%s",
                                provider_name,
                                model,
                                reason_text,
                            )
                        elif (
                            core_valid
                            and generation_profile == "git_challenge"
                            and _git_challenge_safe_blocks_reserve_candidate(q, reasons, course_id)
                        ):
                            logger.info(
                                "[LLM] Git challenge-safe reserve rejected provider=%s model=%s reasons=%s",
                                provider_name,
                                model,
                                reason_text,
                            )
                        if (
                            brittle_policy["risky_generation"]
                            and brittle_validation_failures >= HARD_SHORT_SCOPE_VALIDATION_FAILURE_LIMIT
                        ):
                            raise NoValidQuestionError(
                                "High-difficulty generation became brittle on this narrow source scope.",
                                fallback_context=_build_no_valid_question_context(
                                    policy=brittle_policy,
                                    requested_difficulty=difficulty,
                                    provider_model_attempts=provider_model_attempts,
                                    brittle_validation_failures=brittle_validation_failures,
                                    provider_failures=provider_failures,
                                    rate_limit_failures=rate_limit_failures,
                                    exit_reason="repeated_strict_validation_failures",
                                ),
                            )
                        continue

                except ProviderHTTPError as e:
                    _record_failure_cooldown(e.provider, e.model, e.status_code, e.message)
                    lowered_message = str(e.message or "").lower()
                    if (
                        generation_profile == "git_challenge"
                        and e.provider == "mistral"
                        and e.status_code == 503
                        and any(marker in lowered_message for marker in ("overflow", "overload", "upstream"))
                    ):
                        _record_git_challenge_model_instability(e.provider, e.model, "upstream_503_overflow")
                    msg = f"[LLM] HTTP failure provider={e.provider} model={e.model} status={e.status_code} details={e.message}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    if e.status_code in {429, 503}:
                        provider_failures += 1
                        if e.status_code == 429:
                            rate_limit_failures += 1
                    if (
                        brittle_policy["risky_generation"]
                        and provider_failures >= HARD_SHORT_SCOPE_PROVIDER_FAILURE_LIMIT
                    ):
                        raise NoValidQuestionError(
                            "High-difficulty generation became brittle on this narrow source scope.",
                            fallback_context=_build_no_valid_question_context(
                                policy=brittle_policy,
                                requested_difficulty=difficulty,
                                provider_model_attempts=provider_model_attempts,
                                brittle_validation_failures=brittle_validation_failures,
                                provider_failures=provider_failures,
                                rate_limit_failures=rate_limit_failures,
                                exit_reason="repeated_provider_failures",
                            ),
                        )

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. "
                            f"Check payload/model compatibility. Details: {e.message}"
                        )

                    # fallback to next model/provider
                    continue

                except json.JSONDecodeError as e:
                    if generation_profile == "git_challenge":
                        _record_git_challenge_model_instability(provider_name, model, "json_parse_failure")
                    msg = f"[LLM] JSON parse failed provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    if brittle_policy["risky_generation"]:
                        provider_failures += 1
                        if provider_failures >= HARD_SHORT_SCOPE_PROVIDER_FAILURE_LIMIT:
                            raise NoValidQuestionError(
                                "High-difficulty generation became brittle on this narrow source scope.",
                                fallback_context=_build_no_valid_question_context(
                                    policy=brittle_policy,
                                    requested_difficulty=difficulty,
                                    provider_model_attempts=provider_model_attempts,
                                    brittle_validation_failures=brittle_validation_failures,
                                    provider_failures=provider_failures,
                                    rate_limit_failures=rate_limit_failures,
                                    exit_reason="repeated_invalid_provider_responses",
                                ),
                            )
                    continue

                except NoValidQuestionError:
                    raise

                except Exception as e:
                    if generation_profile == "git_challenge":
                        lowered_error = _safe_exception_text(e).lower()
                        if isinstance(e, httpx.ReadTimeout) or "read timeout" in lowered_error or "timed out" in lowered_error:
                            _record_git_challenge_model_instability(provider_name, model, "read_timeout")
                    msg = f"[LLM] Unexpected error provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    if brittle_policy["risky_generation"]:
                        provider_failures += 1
                        if provider_failures >= HARD_SHORT_SCOPE_PROVIDER_FAILURE_LIMIT:
                            raise NoValidQuestionError(
                                "High-difficulty generation became brittle on this narrow source scope.",
                                fallback_context=_build_no_valid_question_context(
                                    policy=brittle_policy,
                                    requested_difficulty=difficulty,
                                    provider_model_attempts=provider_model_attempts,
                                    brittle_validation_failures=brittle_validation_failures,
                                    provider_failures=provider_failures,
                                    rate_limit_failures=rate_limit_failures,
                                    exit_reason="repeated_unexpected_provider_errors",
                                ),
                            )
                    continue

            if (
                effective_attempt_budget is not None
                and provider_model_attempts >= effective_attempt_budget
            ):
                break

    reserve_brittle_reasons = []
    if reserve_candidate is not None:
        reserve_brittle_reasons = _brittle_reason_subset(
            reserve_candidate.get("strict_reasons", [])
        )
        if brittle_policy["risky_generation"] and reserve_brittle_reasons:
            logger.warning(
                "[LLM] Skipping last resort for hard-short-scope topic=%s difficulty=%s reasons=%s",
                topic,
                difficulty,
                ",".join(reserve_brittle_reasons),
            )
            raise NoValidQuestionError(
                "High-difficulty generation became brittle on this narrow source scope.",
                fallback_context=_build_no_valid_question_context(
                    policy=brittle_policy,
                    requested_difficulty=difficulty,
                    provider_model_attempts=provider_model_attempts,
                    brittle_validation_failures=brittle_validation_failures,
                    provider_failures=provider_failures,
                    rate_limit_failures=rate_limit_failures,
                    exit_reason="skip_last_resort_for_brittle_high_difficulty",
                ),
            )

    if reserve_candidate is not None and not allow_last_resort:
        logger.warning(
            "[LLM] Last resort disabled topic=%s difficulty=%s generation_profile=%s",
            topic,
            difficulty,
            generation_profile,
        )
        raise NoValidQuestionError(
            "All providers/models failed to produce a valid non-reserve question.",
            fallback_context=_build_no_valid_question_context(
                policy=brittle_policy,
                requested_difficulty=difficulty,
                provider_model_attempts=provider_model_attempts,
                brittle_validation_failures=brittle_validation_failures,
                provider_failures=provider_failures,
                rate_limit_failures=rate_limit_failures,
                exit_reason="last_resort_disabled",
            ),
        )

    if reserve_candidate is not None:
        question = _shuffle_question_options(reserve_candidate["question"])
        logger.warning(
            "[LLM] LAST_RESORT_ACCEPT provider=%s model=%s strict_reasons=%s",
            reserve_candidate["provider"],
            reserve_candidate["model"],
            ",".join(reserve_candidate["strict_reasons"]) if reserve_candidate["strict_reasons"] else "unknown",
        )
        if return_metadata:
            return question, _metadata_payload(
                used_last_resort=True,
                provider=reserve_candidate["provider"],
                model=reserve_candidate["model"],
                validation_reasons_at_accept=reserve_candidate.get("strict_reasons", []),
            )
        return question

    raise NoValidQuestionError(
        "All providers/models failed to produce a valid question.\n" + "\n".join(all_failures),
        fallback_context=_build_no_valid_question_context(
            policy=brittle_policy,
            requested_difficulty=difficulty,
            provider_model_attempts=provider_model_attempts,
            brittle_validation_failures=brittle_validation_failures,
            provider_failures=provider_failures,
            rate_limit_failures=rate_limit_failures,
            exit_reason="exhausted_provider_chain",
        ),
    )


async def generate_question_with_metadata(
    topic: str,
    difficulty: int,
    source_text: str,
    *,
    course_id: str | None = None,
    max_provider_model_attempts: int | None = None,
    allow_internal_fallback: bool = True,
    generation_profile: str = "default",
    allow_last_resort: bool = True,
) -> tuple[dict, dict]:
    git_challenge_safe_mode = (
        generation_profile == "git_challenge"
        and is_csen603_git_workflow_scope(course_id, topic, source_text)
    )
    if git_challenge_safe_mode and difficulty >= 5:
        current_difficulty = 4
        attempted_difficulties = [4]
        logger.info(
            "[LLM] Git challenge safe-mode difficulty cap topic=%s requested_difficulty=%s generation_difficulty=%s",
            topic,
            difficulty,
            current_difficulty,
        )
    else:
        attempted_difficulties = [difficulty]
        current_difficulty = difficulty

    git_fragile_topic_guard = (
        difficulty >= 5
        and is_csen603_git_workflow_scope(course_id, topic, source_text)
        and _is_fragile_git_topic_bucket(_git_topic_bucket(topic))
    )

    while True:
        try:
            question, metadata = await _generate_question_once(
                topic,
                current_difficulty,
                source_text,
                course_id=course_id,
                return_metadata=True,
                max_provider_model_attempts=max_provider_model_attempts,
                generation_profile=generation_profile,
                allow_last_resort=allow_last_resort,
            )
            metadata = dict(metadata)
            metadata["requested_difficulty"] = difficulty
            metadata["returned_difficulty"] = int(question.get("difficulty", current_difficulty))
            metadata["attempted_difficulties"] = attempted_difficulties[:]
            metadata["used_difficulty_fallback"] = current_difficulty != difficulty
            return question, metadata
        except NoValidQuestionError as error:
            if not allow_internal_fallback:
                raise ValueError(str(error)) from error

            fallback_context = dict(getattr(error, "fallback_context", {}) or {})
            if git_fragile_topic_guard and current_difficulty >= 5 and 4 not in attempted_difficulties:
                logger.warning(
                    "[LLM] Git fragile-topic difficulty fallback topic=%s requested_difficulty=%s fallback_difficulty=%s exit_reason=%s",
                    topic,
                    difficulty,
                    4,
                    fallback_context.get("exit_reason"),
                )
                attempted_difficulties.append(4)
                current_difficulty = 4
                continue
            if not fallback_context.get("risky_generation"):
                raise ValueError(str(error)) from error

            fallback_candidates = _fallback_difficulty_candidates(
                difficulty,
                attempted_difficulties,
            )
            if not fallback_candidates:
                raise ValueError(str(error)) from error

            next_difficulty = fallback_candidates[0]
            logger.warning(
                "[LLM] Falling back generation topic=%s requested_difficulty=%s current_difficulty=%s fallback_difficulty=%s exit_reason=%s selected_chars=%s context_mode=%s seed_chunks=%s attempts=%s",
                topic,
                difficulty,
                current_difficulty,
                next_difficulty,
                fallback_context.get("exit_reason"),
                fallback_context.get("selected_chars"),
                fallback_context.get("context_mode"),
                fallback_context.get("selected_chunk_count"),
                fallback_context.get("provider_model_attempts"),
            )
            attempted_difficulties.append(next_difficulty)
            current_difficulty = next_difficulty


async def generate_question(
    topic: str,
    difficulty: int,
    source_text: str,
    *,
    course_id: str | None = None,
    generation_profile: str = "default",
    max_provider_model_attempts: int | None = None,
    allow_last_resort: bool = True,
) -> dict:
    question, _ = await generate_question_with_metadata(
        topic,
        difficulty,
        source_text,
        course_id=course_id,
        generation_profile=generation_profile,
        max_provider_model_attempts=max_provider_model_attempts,
        allow_last_resort=allow_last_resort,
    )
    return question

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
- Use an analogy to genuinely clarify the concept.
- Be concise — 2 to 3 sentences maximum.
- Do not introduce new topics or extra facts not already implied by the original explanation.

Respond with ONLY the simpler explanation text, no preamble.
"""

    all_failures = []

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logger.info(f"[LLM] Skipping provider={provider_name} for simpler explanation")
                continue

            cooldown_remaining = _provider_cooldown_remaining(provider_name)
            if cooldown_remaining > 0:
                logger.info(
                    "[LLM] Skipping provider=%s cooldown_remaining=%ss for simpler explanation",
                    provider_name,
                    int(cooldown_remaining),
                )
                continue

            if not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} for simpler explanation")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                model_cooldown_remaining = _model_cooldown_remaining(provider_name, model)
                if model_cooldown_remaining > 0:
                    logger.info(
                        "[LLM] Skipping model cooldown provider=%s model=%s remaining=%ss for simpler explanation",
                        provider_name,
                        model,
                        int(model_cooldown_remaining),
                    )
                    continue

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
                    _record_failure_cooldown(e.provider, e.model, e.status_code, e.message)
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

def _looks_like_jsonish_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    return (
        value.startswith("{") or
        value.startswith("[") or
        '"steps"' in lowered or
        '"title"' in lowered or
        '"intro"' in lowered or
        "```json" in lowered
    )


def _extract_json_object_text(raw: str) -> str | None:
    value = _strip_markdown_fences(str(raw or "")).strip()
    if not value:
        return None
    if value.startswith("{") and value.endswith("}"):
        return value

    start = value.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(value)):
        char = value[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start:index + 1]
    return None


def _fallback_step_by_step_from_context(
    *,
    question: str = "",
    explanation: str = "",
    selected_answer: str = "",
    correct_answer: str = "",
) -> dict:
    question_text = str(question or "").strip()
    explanation_text = str(explanation or "").strip()
    selected_key = str(selected_answer or "").strip()
    correct_key = str(correct_answer or "").strip()
    comparison_text = (
        "Compare your selected answer with the correct answer, then use the explanation to see which option matches the cue."
    )
    if selected_key and correct_key and selected_key != correct_key:
        comparison_text = (
            "Compare the selected option with the correct option. The correct answer follows the cue described in the explanation."
        )

    return {
        "intro": "Here is a clearer way to reason through it:",
        "steps": [
            {
                "title": "Identify the concept",
                "text": (
                    "Start by naming the idea the question is testing."
                    if not question_text
                    else "Start with the concept or situation described in the question."
                )
            },
            {
                "title": "Use the key cue",
                "text": explanation_text or "Use the main clue in the question and compare it with the answer options."
            },
            {
                "title": "Choose the best option",
                "text": comparison_text
            },
        ],
        "takeaway": "The correct answer follows from the key concept explained above."
    }


def _fallback_step_by_step_from_text(text: str, fallback_text: str = "") -> dict:
    if _looks_like_jsonish_text(text):
        return _fallback_step_by_step_from_context(explanation=fallback_text)
    body = str(text or fallback_text or "").strip()
    if not body:
        return _fallback_step_by_step_from_context()
    return _fallback_step_by_step_from_context(explanation=body)


def _normalize_step_by_step_result(result: dict, fallback_text: str) -> dict:
    intro = str(result.get("intro") or "Here is the reasoning path:").strip()
    takeaway = str(result.get("takeaway") or "").strip()
    raw_steps = result.get("steps") or []
    steps = []

    if isinstance(raw_steps, list):
        for index, step in enumerate(raw_steps):
            if isinstance(step, dict):
                title = str(step.get("title") or ("Step " + str(index + 1))).strip()
                text = str(step.get("text") or "").strip()
            else:
                title = "Step " + str(index + 1)
                text = str(step or "").strip()

            if _looks_like_jsonish_text(title) or _looks_like_jsonish_text(text):
                continue

            if title and text:
                steps.append({
                    "title": title[:90],
                    "text": text
                })

    steps = steps[:5]
    if len(steps) < 3:
        return _fallback_step_by_step_from_context(explanation=fallback_text)

    return {
        "intro": intro,
        "steps": steps,
        "takeaway": takeaway or "The key is matching the question cue to the correct concept."
    }


async def generate_step_by_step_explanation(
    topic: str,
    question: str,
    explanation: str,
    *,
    options: dict[str, str] | None = None,
    selected_answer: str = "",
    correct_answer: str = "",
) -> dict:
    selected_key = str(selected_answer or "").strip()
    correct_key = str(correct_answer or "").strip()
    options = options or {}
    selected_text = str(options.get(selected_key) or "").strip()
    correct_text = str(options.get(correct_key) or "").strip()
    serialized_options = json.dumps(options, ensure_ascii=False)

    prompt = f"""
Create a compact step-by-step explanation for a learner.

Topic: {topic}
Question: {question}
Options: {serialized_options}
Selected answer key: {selected_key}
Selected answer text: {selected_text}
Correct answer key: {correct_key}
Correct answer text: {correct_text}
Standard explanation: {explanation}

Return ONLY valid JSON with this shape:
{{
  "intro": "Here is the reasoning path:",
  "steps": [
    {{"title": "Identify the concept", "text": "..."}},
    {{"title": "Notice the key cue", "text": "..."}},
    {{"title": "Choose the best option", "text": "..."}}
  ],
  "takeaway": "..."
}}

Rules:
- Use 3 to 5 short steps.
- Focus on why the correct answer fits.
- If the selected answer is wrong, compare it with the correct answer without being harsh.
- Keep everything grounded in the question, options, and standard explanation.
- Do not introduce new facts beyond the provided context.
- Keep the language student-friendly and concise.
"""

    all_failures = []

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logger.info(f"[LLM] Skipping provider={provider_name} for step_by_step_explanation")
                continue

            cooldown_remaining = _provider_cooldown_remaining(provider_name)
            if cooldown_remaining > 0:
                logger.info(
                    "[LLM] Skipping provider=%s cooldown_remaining=%ss for step_by_step_explanation",
                    provider_name,
                    int(cooldown_remaining),
                )
                continue

            if not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} for step_by_step_explanation")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                model_cooldown_remaining = _model_cooldown_remaining(provider_name, model)
                if model_cooldown_remaining > 0:
                    logger.info(
                        "[LLM] Skipping model cooldown provider=%s model=%s remaining=%ss for step_by_step_explanation",
                        provider_name,
                        model,
                        int(model_cooldown_remaining),
                    )
                    continue

                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw).strip()

                    json_text = _extract_json_object_text(raw)
                    try:
                        result = json.loads(json_text or raw)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[LLM] JSON parse fallback step_by_step_explanation provider=%s model=%s",
                            provider_name,
                            model,
                        )
                        return _fallback_step_by_step_from_context(
                            question=question,
                            explanation=explanation,
                            selected_answer=selected_answer,
                            correct_answer=correct_answer,
                        )

                    normalized = _normalize_step_by_step_result(result, explanation)
                    logger.info(
                        "[LLM] SUCCESS step_by_step_explanation provider=%s model=%s",
                        provider_name,
                        model,
                    )
                    return normalized

                except ProviderHTTPError as e:
                    _record_failure_cooldown(e.provider, e.model, e.status_code, e.message)
                    msg = f"[LLM] HTTP failure step_by_step_explanation provider={e.provider} model={e.model} status={e.status_code} details={e.message}"
                    logger.warning(msg)
                    all_failures.append(msg)

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. Details: {e.message}"
                        )

                    continue

                except Exception as e:
                    msg = f"[LLM] Unexpected step_by_step_explanation error provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

    raise ValueError("All providers/models failed to produce a step-by-step explanation.\n" + "\n".join(all_failures))


async def generate_worked_example_support(
    *,
    topic: str,
    question: str,
    options: dict[str, str],
    correct_answer: str,
    explanation: str,
) -> dict:
    correct_key = str(correct_answer or "").strip()
    correct_text = str((options or {}).get(correct_key) or "").strip()
    serialized_options = json.dumps(options or {}, ensure_ascii=False)
    prompt = f"""
Create a compact worked example primer for a student who is recovering from a wrong answer.

Topic: {topic}
Example question: {question}
Options: {serialized_options}
Correct answer key: {correct_key}
Correct answer text: {correct_text}
Original explanation: {explanation}

Return ONLY valid JSON with this shape:
{{
  "intro_text": "Here’s a solved example before you try again.",
  "worked_steps": ["...", "...", "..."],
  "tempting_note": "..."
}}

Rules:
- Keep it compact and supportive.
- "worked_steps" must contain 2 to 4 short steps.
- Each step should reflect the reasoning path, not just restate the answer.
- Keep everything grounded in the provided question and explanation.
- "tempting_note" is optional, but if included it should be one short sentence.
- Do not mention answer letters inside the steps unless absolutely necessary.
- Do not introduce new facts beyond what is already implied by the explanation.
"""

    all_failures = []

    async with httpx.AsyncClient(timeout=30) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logger.info(f"[LLM] Skipping provider={provider_name} for worked example support")
                continue

            cooldown_remaining = _provider_cooldown_remaining(provider_name)
            if cooldown_remaining > 0:
                logger.info(
                    "[LLM] Skipping provider=%s cooldown_remaining=%ss for worked example support",
                    provider_name,
                    int(cooldown_remaining),
                )
                continue

            if not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} for worked example support")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                model_cooldown_remaining = _model_cooldown_remaining(provider_name, model)
                if model_cooldown_remaining > 0:
                    logger.info(
                        "[LLM] Skipping model cooldown provider=%s model=%s remaining=%ss for worked example support",
                        provider_name,
                        model,
                        int(model_cooldown_remaining),
                    )
                    continue

                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw).strip()
                    result = json.loads(raw)

                    intro_text = str(
                        result.get("intro_text") or "Here’s a solved example before you try again."
                    ).strip()
                    worked_steps = [
                        str(step or "").strip()
                        for step in (result.get("worked_steps") or [])
                        if str(step or "").strip()
                    ][:4]
                    tempting_note = str(result.get("tempting_note") or "").strip()

                    if len(worked_steps) < 2:
                        msg = (
                            f"[LLM] Worked example support missing steps "
                            f"provider={provider_name} model={model}"
                        )
                        logger.warning(msg)
                        all_failures.append(msg)
                        continue

                    logger.info(
                        "[LLM] SUCCESS worked_example_support provider=%s model=%s",
                        provider_name,
                        model,
                    )
                    return {
                        "intro_text": intro_text,
                        "worked_steps": worked_steps,
                        "tempting_note": tempting_note or None,
                    }

                except ProviderHTTPError as e:
                    _record_failure_cooldown(e.provider, e.model, e.status_code, e.message)
                    msg = (
                        f"[LLM] HTTP failure worked_example_support "
                        f"provider={e.provider} model={e.model} status={e.status_code} details={e.message}"
                    )
                    logger.warning(msg)
                    all_failures.append(msg)

                    if e.status_code == 400:
                        raise ValueError(
                            f"Bad request sent to {e.provider}/{e.model}. Details: {e.message}"
                        )

                    continue

                except Exception as e:
                    msg = (
                        f"[LLM] Unexpected worked_example_support error "
                        f"provider={provider_name} model={model} error={_safe_exception_text(e)}"
                    )
                    logger.warning(msg)
                    all_failures.append(msg)
                    continue

    raise ValueError(
        "All providers/models failed to produce worked example support.\n" + "\n".join(all_failures)
    )

async def extract_content_metadata(sample_text: str) -> dict:
    """
    Use the LLM fallback chain to extract metadata only from lecture text.
    Returns:
      course_name, suggested_title, suggested_week, topics, summary
    """
    prompt = CONTENT_EXTRACTION_PROMPT.format(text=sample_text)

    all_failures = []

    async with httpx.AsyncClient(timeout=60) as client:
        for provider_name in PROVIDER_ORDER:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                logger.info(f"[LLM] Skipping provider={provider_name} for content metadata")
                continue

            cooldown_remaining = _provider_cooldown_remaining(provider_name)
            if cooldown_remaining > 0:
                logger.info(
                    "[LLM] Skipping provider=%s cooldown_remaining=%ss for content metadata",
                    provider_name,
                    int(cooldown_remaining),
                )
                continue

            if not provider_cfg["api_key"]:
                logger.info(f"[LLM] Skipping provider={provider_name} for content metadata")
                continue

            models = PROVIDER_MODELS.get(provider_name, [])
            for model in models:
                model_cooldown_remaining = _model_cooldown_remaining(provider_name, model)
                if model_cooldown_remaining > 0:
                    logger.info(
                        "[LLM] Skipping model cooldown provider=%s model=%s remaining=%ss for content metadata",
                        provider_name,
                        model,
                        int(model_cooldown_remaining),
                    )
                    continue

                try:
                    raw = await _call_model(client, provider_name, model, prompt)
                    raw = _strip_markdown_fences(raw).strip()

                    result = json.loads(raw)

                    required = ["course_name", "suggested_title", "suggested_week", "topics", "summary"]
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

                    result["course_name"] = str(result.get("course_name") or "").strip()

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
                    _record_failure_cooldown(e.provider, e.model, e.status_code, e.message)
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
