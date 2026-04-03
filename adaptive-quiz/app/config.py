import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

if not MONGODB_URI:
    raise ValueError("Missing MONGODB_URI")

if not any([OPENROUTER_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, HF_TOKEN]):
    raise ValueError(
        "At least one provider key is required: "
        "GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY, or HF_TOKEN"
    )