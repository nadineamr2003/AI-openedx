import argparse
import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.mongodb import close_db, connect_db, get_db  # noqa: E402
from app.routers.quiz import (  # noqa: E402
    CACHE_PROMPT_VERSION,
    _git_challenge_bank_target,
    _make_source_scope_key,
    _replenish_cache,
)
from app.services.ai_engine import is_csen603_git_workflow_scope  # noqa: E402


DEFAULT_COURSE_ID = "csen603"


async def _git_content_items(course_id: str) -> list[dict]:
    db = get_db()
    docs = await db.course_content.find(
        {
            "course_id": course_id,
            "active": True,
        }
    ).to_list(length=100)

    items: list[dict] = []
    for doc in docs:
        source_text = str(doc.get("source_text") or "").strip()
        topics = [
            str(topic).strip()
            for topic in (doc.get("topics") or [])
            if str(topic).strip()
        ]
        if not source_text or not topics:
            continue
        if not any(is_csen603_git_workflow_scope(course_id, topic, source_text) for topic in topics):
            continue
        items.append(
            {
                "source_text": source_text,
                "source_scope_key": _make_source_scope_key(source_text),
                "topics": topics,
            }
        )
    return items


async def purge_git_cache(course_id: str) -> int:
    db = get_db()
    git_items = await _git_content_items(course_id)
    source_scope_keys = [item["source_scope_key"] for item in git_items]
    git_topics = sorted({topic for item in git_items for topic in item["topics"]})

    delete_query = {"course_id": course_id}
    or_clauses = []
    if git_topics:
        or_clauses.append({"topic": {"$in": git_topics}})
    if source_scope_keys:
        or_clauses.append({"source_scope_key": {"$in": source_scope_keys}})
    if not or_clauses:
        return 0
    delete_query["$or"] = or_clauses

    result = await db.questions_cache.delete_many(delete_query)
    return int(result.deleted_count or 0)


async def prefill_git_challenge_bank(course_id: str) -> list[tuple[str, int]]:
    git_items = await _git_content_items(course_id)
    topic_to_source: dict[str, tuple[str, str]] = {}
    for item in git_items:
        for topic in item["topics"]:
            topic_to_source.setdefault(
                topic,
                (item["source_text"], item["source_scope_key"]),
            )

    warmed: list[tuple[str, int]] = []
    for topic, (source_text, source_scope_key) in topic_to_source.items():
        target = _git_challenge_bank_target(topic)
        if target <= 0:
            continue
        await _replenish_cache(
            topic=topic,
            difficulty=4,
            course_id=course_id,
            source_text=source_text,
            source_scope_key=source_scope_key,
            prompt_version=CACHE_PROMPT_VERSION,
            target=target,
            mode="challenge",
            git_workflow_scope=True,
        )
        warmed.append((topic, target))
    return warmed


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Purge and optionally prefill the CSEN603 Git challenge-safe cache bank.")
    parser.add_argument("--course-id", default=DEFAULT_COURSE_ID, help="Course id to maintain. Default: csen603")
    parser.add_argument("--purge-only", action="store_true", help="Only purge the Git cache entries.")
    parser.add_argument("--prefill", action="store_true", help="Prefill the Git challenge-safe bank after purge.")
    args = parser.parse_args()

    await connect_db()
    try:
        deleted = await purge_git_cache(args.course_id)
        print(f"Purged {deleted} Git cache entries for course_id={args.course_id}.")

        should_prefill = args.prefill or not args.purge_only
        if should_prefill:
            warmed = await prefill_git_challenge_bank(args.course_id)
            if warmed:
                for topic, target in warmed:
                    print(f"Prefill requested topic={topic} target={target} difficulty=4")
            else:
                print("No active Git lecture content was found to prefill.")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(_main())
