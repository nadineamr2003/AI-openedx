import asyncio
from app.services.ai_engine import generate_question

SOURCE_TEXT = """
A recursive function is a function that calls itself in order to solve a problem.
Every recursive function must have a base case — a condition that stops the recursion.
Without a base case, the function would call itself infinitely, causing a stack overflow.
The classic example is the factorial function: factorial(n) = n * factorial(n-1),
with the base case factorial(0) = 1.
"""

async def main():
    print("Generating question...\n")
    q = await generate_question(
        topic="recursion",
        difficulty=2,
        source_text=SOURCE_TEXT
    )
    print(f"Question:  {q['question']}")
    print(f"Options:   {q['options']}")
    print(f"Answer:    {q['correct_answer']}")
    print(f"Explanation: {q['explanation']}")
    print(f"Difficulty: {q['difficulty']}")

asyncio.run(main())