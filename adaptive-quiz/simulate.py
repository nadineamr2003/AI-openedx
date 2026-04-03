import sys
sys.path.insert(0, ".")

from app.services.adaptation import get_initial_student_state, process_answer, select_next_topic

# Simulate a student answering 15 questions
topics = ["recursion", "sorting", "linked_lists"]
state = get_initial_student_state("student_001", "CS101", topics)

# Simulated answers: (topic, correct, time_ms)
answers = [
    ("recursion",    True,  18000),
    ("sorting",      False, 45000),
    ("recursion",    True,  15000),
    ("linked_lists", False, 50000),
    ("sorting",      False, 38000),
    ("recursion",    True,  12000),
    ("recursion",    True,  10000),
    ("sorting",      True,  25000),
    ("linked_lists", True,  20000),
    ("sorting",      True,  18000),
    ("linked_lists", False, 42000),
    ("recursion",    True,   9000),
    ("sorting",      True,  15000),
    ("linked_lists", True,  22000),
    ("linked_lists", True,  19000),
]

print(f"{'Q':<4} {'Topic':<15} {'Result':<8} {'Time(s)':<10} {'Mastery':<10} {'Difficulty':<12} {'IRT Active'}")
print("-" * 75)

for i, (topic, correct, time_ms) in enumerate(answers, 1):
    state = process_answer(state, topic, correct, time_ms)
    next_topic, next_mode = select_next_topic(state["topic_mastery"])
    print(
        f"{i:<4} {topic:<15} {'✅' if correct else '❌':<8} "
        f"{time_ms/1000:<10.1f} "
        f"{state['topic_mastery'][topic]:<10.4f} "
        f"{state['current_difficulty']:<12} "
        f"{'Yes' if state['irt_active'] else 'No'}"
    )

print("\n--- Final Mastery Scores ---")
for topic, mastery in state["topic_mastery"].items():
    bar = "█" * int(mastery * 20)
    print(f"{topic:<15} {mastery:.4f}  {bar}")

print(f"\nNext recommended topic: {next_topic} ({next_mode})")