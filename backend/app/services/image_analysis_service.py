"""
Per-image vision analysis: extract structured knowledge from a cropped document image
via the Groq / multimodal VLM API (OpenAI-compatible /chat/completions). Used by the
ingestion images stage. Combines the raw image with raw OCR text to produce a
retrieval-ready structured extraction and a routing decision (detected_store).

The VLM prompt is assembled dynamically at import time via
``_build_prompt()`` so the per-store JSON schema instructions (``schema_hint``
strings) are sourced from ``store_router.build_vlm_schema_block()`` — the
single source of truth.  Parsers in store_router derive from the same data,
so the prompt and parsers can never drift.
"""
import base64
import json
import logging
import re

import httpx

from app.services.store_router import build_vlm_schema_block
from app.services.image_router import decide_route

logger = logging.getLogger(__name__)


def _repair_json_newlines(text: str) -> str:
    """Replace literal newlines/tabs inside JSON string values with their escape
    sequences. LLMs often emit actual control characters inside strings, which
    makes the JSON technically invalid and trips json.loads()."""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\":
            result.append(ch)
            escape_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)


def _extract_first_json_object(text: str) -> dict:
    """Parse the first JSON object out of an LLM reply, tolerating:
    - markdown fences (```json … ```)
    - trailing prose after the closing brace
    - literal newlines inside string values (common LLM habit)
    """
    # Strip ALL variants of code fences (```json, ```, ~~~) then stray backticks
    cleaned = re.sub(r"```(?:json|JSON)?|~~~", "", text or "")
    cleaned = cleaned.strip().strip("`").strip()
    start = cleaned.find("{")
    if start == -1:
        return {}
    json_str = cleaned[start:]
    # Pass 1: try as-is
    try:
        obj, _ = json.JSONDecoder().raw_decode(json_str)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    # Pass 2: repair literal control characters inside string values
    try:
        repaired = _repair_json_newlines(json_str)
        obj, _ = json.JSONDecoder().raw_decode(repaired)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _canonical_store(raw: str) -> str:
    """Map VLM free-text store name to a canonical snake_case store identifier."""
    lower = raw.lower()
    if "table" in lower:
        return "table_store"
    if "clause" in lower:
        return "clause_store"
    if (
        "document" in lower
        or "normal" in lower
        or "chunk" in lower
        or "text" in lower
        or "vector" in lower
    ):
        return "vector_store"
    if "image" in lower:
        return "image_store"
    return "image_store"


def _content_type_from_store(store: str) -> str:
    """Derive a content_type string from a canonical store name."""
    mapping = {
        "table_store": "table",
        "vector_store": "text",
        "clause_store": "text",
        "image_store": "figure",
    }
    return mapping.get(store, "figure")


def _build_prompt() -> str:
    """Assemble the full VLM system prompt at module load time.

    The per-store structured_content schema instructions are sourced from
    ``store_router.build_vlm_schema_block()`` so the prompt and the
    downstream parsers (store_router handlers) share the same schema_hint
    strings and can never drift out of sync.

    Returns
    -------
    str
        The complete prompt string sent to the multimodal VLM API.
    """
    base = (
        "You are an expert multimodal document understanding model responsible for extracting "
        "knowledge from document images for a Retrieval-Augmented Generation (RAG) system.\n"
        "Your primary objective is NOT to summarize the image. Your responsibility is to extract "
        "every meaningful piece of information as accurately and completely as possible because "
        "future semantic search, retrieval, and question answering will depend entirely on this "
        "extracted knowledge.\n"
        "You will receive:\n"
        "- The original image.\n"
        "- The raw OCR output.\n"
        "Treat the OCR output only as supporting evidence. It may contain OCR mistakes, incorrect "
        "ordering, missing words, formatting issues, or recognition errors.\n"
        "Carefully compare the OCR text with the image and use both sources together to reconstruct "
        "the most accurate understanding of the document.\n"
        "Deeply analyze the image and preserve every meaningful detail, including but not limited to: "
        "Titles, Headings, Paragraphs, Lists, Tables, Table structure, Key-value pairs, Figure labels, "
        "Diagram relationships, Charts, Mathematical expressions, Symbols, Units, Measurements, Dates, "
        "Entities, Relationships, Captions, Annotations, Domain-specific information, and any semantic "
        "information useful for future retrieval.\n"
        "If OCR misses information that is visible in the image, recover it from the image.\n"
        "If OCR contains mistakes, use the image to understand the correct meaning while leaving the "
        "raw OCR unchanged in storage.\n"
        "Do NOT generate only a caption or short summary.\n"
        "Generate a rich structured extraction that preserves the complete meaning of the image and "
        "contains sufficient information for future RAG retrieval without needing to process the "
        "original image again.\n"
        "After understanding the content, determine the single most appropriate destination store.\n"
        "Possible stores include: Normal Chunk Store, Table Store, Clause Store, "
        "Image Store.\n"
        "The destination should be selected based on the extracted content, not merely the image type.\n"
        "- Normal Chunk Store: general policy/procedural text, paragraphs, prose.\n"
        "- Table Store: structured tabular data with rows and columns.\n"
        "- Clause Store: legal clauses, contractual obligations, terms.\n"
        "- Image Store: pure figures, diagrams, charts, illustrations with no dominant text.\n"
    )

    schema_block = build_vlm_schema_block()

    response_format = (
        "Respond with ONLY a JSON object — no markdown fences, no extra text — with exactly "
        "these keys:\n"
        '{"detected_store": "...", "structured_content": {...}, "ocr_text": "...", '
        '"confidence": 0.0, "reason_for_store_selection": "..."}\n'
        'detected_store = one of exactly: "Normal Chunk Store", "Table Store", "Clause Store", '
        '"Image Store".\n'
        "structured_content = a JSON *object* (not a string, not prose) whose shape matches the "
        "schema for the chosen detected_store as listed above. The object must be fully populated "
        "with every key the destination store expects. Do NOT produce a plain-text string here — "
        "the downstream parser expects a JSON object. If the content genuinely cannot be "
        "structured, fall back to a minimal object with at least one text field.\n"
        "ocr_text = ALL text visible in the image, transcribed verbatim by you reading the image "
        "directly. Use the raw OCR only to help disambiguate hard-to-read characters; this is "
        "your own corrected, complete transcription of the visible text (empty string if the "
        "image has no text).\n"
        "confidence = a number between 0 and 1 for your store selection.\n"
        "reason_for_store_selection = a brief justification."
    )

    return "\n".join([base, schema_block, response_format])


#: Full VLM prompt assembled once at module import. The schema block is
#: sourced from store_router.build_vlm_schema_block() so prompt and parsers
#: share the same schema_hint strings and cannot drift.
_VLM_PROMPT: str = _build_prompt()


def _bound_ocr_text(raw_ocr_text: str, max_chars: int) -> str:
    """Truncate OCR text to at most ``max_chars`` characters before it is
    appended to the VLM prompt.

    An unbounded OCR dump (e.g. a dense multi-column page mis-cropped as a
    single "image") can overflow the model's context window and silently
    truncate the actual instructions earlier in the prompt. Keep the head of
    the OCR text (usually the most relevant portion for a small crop) and
    append a short marker when truncation occurs.
    """
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        # Misconfigured/mocked setting — fail open (no truncation) rather than
        # crash the whole VLM call.
        return raw_ocr_text

    if max_chars <= 0 or len(raw_ocr_text) <= max_chars:
        return raw_ocr_text

    kept = raw_ocr_text[:max_chars]
    logger.info(
        "analyze_image: OCR text truncated for VLM prompt (original=%d chars, kept=%d chars)",
        len(raw_ocr_text), max_chars,
    )
    return kept + "\n…[truncated]"


def analyze_image(png_bytes: bytes, raw_ocr_text: str) -> dict:
    """Extract structured knowledge from a cropped document image for RAG ingestion.

    Sends both the image and the raw OCR text to the multimodal VLM. The VLM
    performs a rich, retrieval-ready extraction and determines the appropriate destination
    store. The raw OCR is included as supporting evidence so the model can reconcile OCR
    errors against what it sees in the image.

    Args:
        png_bytes:    PNG image bytes of the cropped document region.
        raw_ocr_text: Raw OCR text already extracted from the same region (may be empty).

    Returns:
        A dict with keys:
            structured_content       – rich extraction text (never empty if VLM succeeded)
            vlm_ocr_text             – VLM's own verbatim transcription of the image text
            detected_store           – canonical store name (snake_case)
            confidence               – float in [0, 1]
            reason_for_store_selection – brief justification string
            content_type             – derived from detected_store
    """
    from app.config import settings

    # analyze_image only runs for KEPT (informative) images. With no valid VLM
    # decision we route purely by content: OCR text that is genuinely searchable
    # goes to vector_store; if there is no meaningful text the image stays a pure
    # repository asset in image_store (never orphaned informative content, never
    # forced noise into vector_store).
    _fb_route = decide_route(
        canonical_store="image_store",
        structured_content=raw_ocr_text or "", ocr_text=raw_ocr_text or "",
        confidence=0.0, vlm_succeeded=False, base_reason="VLM unavailable",
    )
    _fallback = {
        "structured_content": raw_ocr_text or "",
        "vlm_ocr_text": "",
        "detected_store": _fb_route.destination_store,
        "confidence": 0.0,
        "reason_for_store_selection": _fb_route.reason,
        "content_type": _fb_route.content_type,
        "content_class": _fb_route.content_class,
        "extraction_quality": _fb_route.extraction_quality,
    }

    if not settings.GROQ_BASE_URL:
        logger.warning("GROQ_BASE_URL not set — skipping image analysis")
        return _fallback

    try:
        b64 = base64.b64encode(png_bytes).decode()
        headers = {"Content-Type": "application/json"}
        if settings.GROQ_API_KEY:
            headers["Authorization"] = f"Bearer {settings.GROQ_API_KEY}"

        bounded_ocr_text = _bound_ocr_text(raw_ocr_text or "", settings.VLM_OCR_MAX_CHARS)

        ocr_section = (
            "\n\n--- RAW OCR OUTPUT (supporting evidence, may contain errors) ---\n"
            + bounded_ocr_text
        )
        full_prompt = _VLM_PROMPT + ocr_section

        model_name = getattr(settings, "GROQ_VLM_MODEL", None) or "llama-3.2-11b-vision-preview"
        timeout_seconds = getattr(settings, "VLM_TIMEOUT_SECONDS", 12.0)

        payload = {
            "model": model_name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": full_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            "max_tokens": settings.VLM_MAX_TOKENS,
            "temperature": 0.0,
        }

        base = settings.GROQ_BASE_URL.rstrip("/")
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(f"{base}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()

        raw_resp = resp.json()
        content = raw_resp["choices"][0]["message"]["content"].strip()
        logger.debug("analyze_image raw response (first 300): %s", content[:300])

        data = _extract_first_json_object(content)

        # --- structured_content ---
        raw_sc = data.get("structured_content", "")
        if isinstance(raw_sc, (dict, list)):
            structured_content = json.dumps(raw_sc, ensure_ascii=False, indent=2)
        else:
            structured_content = str(raw_sc).strip()

        if not structured_content:
            # JSON parse failed entirely — fall back to stripped raw response text
            structured_content = re.sub(r"```(?:json|JSON)?|~~~|`", "", content).strip()
            logger.warning(
                "analyze_image: structured_content empty after JSON parse; "
                "storing raw VLM text. Raw (first 200): %r",
                content[:200],
            )

        # --- vlm_ocr_text (VLM's own verbatim transcription, distinct from raw OCR) ---
        raw_vlm_ocr = data.get("ocr_text", "")
        if isinstance(raw_vlm_ocr, (dict, list)):
            vlm_ocr_text = json.dumps(raw_vlm_ocr, ensure_ascii=False)
        else:
            vlm_ocr_text = str(raw_vlm_ocr).strip()

        # --- confidence ---
        try:
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        # --- reason_for_store_selection ---
        reason = str(data.get("reason_for_store_selection", "")).strip()

        # --- routing decision (explicit, content-driven) ---
        # A real store the VLM picked wins; an explicit "image" verdict is honoured
        # (non-semantic figure -> repository); otherwise route by whether the
        # extracted content is genuinely searchable. See image_router.decide_route.
        raw_store = str(data.get("detected_store", "")).strip()
        canonical = _canonical_store(raw_store) if raw_store else "image_store"
        route = decide_route(
            canonical_store=canonical,
            structured_content=structured_content,
            ocr_text=vlm_ocr_text or raw_ocr_text or "",
            confidence=confidence,
            vlm_succeeded=True,
            base_reason=reason,
        )

        return {
            "structured_content": structured_content,
            "vlm_ocr_text": vlm_ocr_text,
            "detected_store": route.destination_store,
            "confidence": route.confidence,
            "reason_for_store_selection": route.reason,
            "content_type": route.content_type,
            "content_class": route.content_class,
            "extraction_quality": route.extraction_quality,
        }

    except Exception as exc:
        logger.warning("analyze_image failed (%s: %s)", type(exc).__name__, exc)
        return _fallback
