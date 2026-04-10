import base64
import logging
import math
import re

logger = logging.getLogger(__name__)

LLM_SAMPLE_CHARS = 14000
PAGES_FOR_SAMPLE = 10


def _looks_like_person_name(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    words = line.split()
    # Person names are 2-3 words max; longer is likely a heading
    if len(words) > 3:
        return False
    if re.match(r"^(Dr\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$", line):
        return True
    return False


def _clean_extracted_text(text: str) -> str:
    """
    Deterministic cleanup only.
    No paraphrasing, no semantic rewriting.
    """
    if not text:
        return ""

    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = []

    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()

        if not stripped:
            cleaned.append("")
            continue

        # remove isolated page numbers
        if re.fullmatch(r"\d+", stripped):
            continue

        # remove emails / urls
        if "@" in stripped:
            continue
        if lowered.startswith("http://") or lowered.startswith("https://"):
            continue

        # remove repeated footer/header fragments
        if lowered.startswith("©") and "all rights reserved" in lowered:
            continue
        if "cisco confidential" in lowered:
            continue
        if "guc 2025" in lowered:
            continue

        # remove instructor/staff roster lines
        if lowered.startswith("dr."):
            continue
        if _looks_like_person_name(stripped):
            continue

        cleaned.append(stripped)

    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_low_value_page(text: str, page_num: int, total_pages: int) -> bool:
    """
    Skip pages that are not useful for quiz content or topic extraction.
    """
    lowered = text.lower()

    low_value_markers = [
        "thank you",
        "communication rules",
        "course resources",
        "course grade distribution",
        "office hours",
        "piazza",
        "access code",
        "weekly dilbert",
        "self study",
        "exercise",
        "email only for crucial issues",
        "final exam",
        "midterm exam",
        "project,",
    ]

    if any(marker in lowered for marker in low_value_markers):
        return True

    # title/cover page with many names but little content
    if page_num == 1:
        short_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(short_lines) <= 6:
            return True
        name_like = sum(1 for ln in short_lines if _looks_like_person_name(ln))
        if name_like >= 3:
            return True

    # near-end acknowledgements/admin pages
    if page_num >= total_pages - 1 and ("thank you" in lowered or "@" in lowered):
        return True

    return False


def _pick_evenly_spaced_pages(pages: list[dict], max_pages: int) -> list[dict]:
    if len(pages) <= max_pages:
        return pages

    if max_pages <= 1:
        return [pages[0]]

    selected = []
    for i in range(max_pages):
        idx = round(i * (len(pages) - 1) / (max_pages - 1))
        selected.append(pages[idx])

    # dedupe if rounding repeated an index
    deduped = []
    seen = set()
    for page in selected:
        if page["page_num"] not in seen:
            seen.add(page["page_num"])
            deduped.append(page)

    return deduped


def extract_text_from_pdf_base64(pdf_base64: str) -> dict:
    """
    Extract text from a base64 PDF using PyMuPDF.

    Returns:
      {
        "raw_text": cleaned content-focused extracted text,
        "page_count": int,
        "sample_text": representative lecture sample for LLM metadata extraction,
        "is_empty": bool
      }
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError(
            "PyMuPDF is not installed. Run: pip install pymupdf"
        )

    try:
        pdf_bytes = base64.b64decode(pdf_base64)
    except Exception as e:
        raise RuntimeError(f"Failed to decode base64 PDF: {e}")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF: {e}")

    raw_pages = []
    total_pages = len(doc)

    for i, page in enumerate(doc, start=1):
        text = page.get_text("text").strip()
        cleaned = _clean_extracted_text(text)
        if cleaned:
            raw_pages.append({
                "page_num": i,
                "text": cleaned,
                "low_value": _is_low_value_page(cleaned, i, total_pages),
            })

    doc.close()

    if not raw_pages:
        return {
            "raw_text": "",
            "page_count": total_pages,
            "sample_text": "",
            "is_empty": True,
        }

    # Content-focused source_text for storage / question generation
    content_pages = [p for p in raw_pages if not p["low_value"]]
    if not content_pages:
        content_pages = raw_pages

    stored_pages = content_pages

    raw_text = "\n\n".join(
        f"[Page {p['page_num']}]\n{p['text']}"
        for p in stored_pages
    ).strip()

    # Metadata sample: keep page 1 if it carries title/week info, then sample broadly
    sample_candidates = []
    page1 = next((p for p in raw_pages if p["page_num"] == 1), None)
    if page1 and page1["text"]:
        sample_candidates.append(page1)

    remaining = [p for p in content_pages if p["page_num"] != 1]
    remaining = _pick_evenly_spaced_pages(remaining, PAGES_FOR_SAMPLE - len(sample_candidates))
    sample_candidates.extend(remaining)

    sample_parts = []
    chars_used = 0
    for p in sample_candidates:
        chunk = f"[Page {p['page_num']}]\n{p['text']}"
        remaining_chars = LLM_SAMPLE_CHARS - chars_used
        if remaining_chars <= 0:
            break
        chunk = chunk[:remaining_chars]
        sample_parts.append(chunk)
        chars_used += len(chunk)

    sample_text = "\n\n".join(sample_parts).strip()

    logger.info(
        "[PDF] Extracted %s/%s non-empty pages, %s content pages kept, %s chars total, %s chars sampled for LLM",
        len(raw_pages),
        total_pages,
        len(content_pages),
        len(raw_text),
        len(sample_text),
    )

    return {
        "raw_text": raw_text,
        "page_count": total_pages,
        "sample_text": sample_text,
        "is_empty": False,
    }