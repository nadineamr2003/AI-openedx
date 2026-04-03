import httpx
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

MODEL_CANDIDATES = [
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemma-3-4b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "openrouter/free",
]

async def call_model(client, key, model):
    resp = await client.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "user", "content": "Say hello in one sentence."}
            ],
        },
        timeout=30,
    )

    data = resp.json()
    return resp.status_code, data

async def test():
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        print("❌ No API key found — check your .env file")
        return

    async with httpx.AsyncClient() as client:
        for model in MODEL_CANDIDATES:
            print(f"\nTrying: {model}")
            status, result = await call_model(client, key, model)

            print("HTTP", status)
            print(result)

            if "choices" in result and result["choices"]:
                print("✅ LLM response:", result["choices"][0]["message"]["content"])
                return

            if "error" in result:
                print("❌ OpenRouter error:", result["error"]["message"])
                continue

        print("\n❌ All model attempts failed.")

asyncio.run(test())