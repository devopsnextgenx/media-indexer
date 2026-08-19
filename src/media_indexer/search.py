import logging
import difflib
import os
import re
from sentence_transformers import SentenceTransformer
from media_indexer.config import settings
from media_indexer.database import db_instance
from media_indexer.utils import (
    AUDIO_EXTENSIONS,
    TICKS_PER_SECOND,
    format_duration,
    format_file_size,
    normalize_text,
    quality_label,
)

logger = logging.getLogger(__name__)

_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def _to_seconds(value) -> float:
    """Accepts seconds (new payloads) or an hh:mm:ss / mm:ss string (legacy payloads)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _CLOCK_RE.match(value.strip())
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return 0.0


def _to_int(value) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _is_audio(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() in AUDIO_EXTENSIONS


def _folder_tags(payload: dict, file_path: str, audio: bool) -> list[str]:
    """Ancestor directory names ordered parent -> grandparent -> great-grandparent."""
    source = payload.get("relative_path") or file_path or ""
    parts = [part for part in re.split(r"[\\/]+", source) if part and part != "."]
    ancestors = parts[:-1]
    if not ancestors:
        return []
    depth = 3 if audio else 2
    return list(reversed(ancestors[-depth:]))


class SemanticSearchEngine:
    def __init__(self):
        self.model = SentenceTransformer(settings.embedding.model_name)

    def _build_metadata(self, meta: dict, jf: dict, audio: bool) -> dict:
        width = _to_int(meta.get("width") or jf.get("width"))
        height = _to_int(meta.get("height") or jf.get("height"))
        size = _to_int(meta.get("size") or meta.get("file_size") or jf.get("size"))
        ticks = _to_int(meta.get("runtime_ticks") or jf.get("runtime_ticks"))

        seconds = _to_seconds(meta.get("duration"))
        if not seconds and ticks:
            seconds = ticks / TICKS_PER_SECOND
        if not seconds:
            seconds = _to_seconds(jf.get("duration"))

        resolution = f"{width}x{height}" if width and height else ("Audio" if audio else "N/A")

        return {
            **meta,
            "width": width,
            "height": height,
            "resolution": resolution,
            "quality": "Audio" if audio else quality_label(width, height),
            "duration": round(seconds, 2),
            "duration_formatted": format_duration(seconds),
            "runtime_ticks": ticks or (int(seconds * TICKS_PER_SECOND) if seconds else None),
            "size": size,
            "file_size": size,
            "file_size_human": format_file_size(size),
        }

    def search(self, query: str, limit: int = 50, min_score: float = 0.55) -> list[dict]:
        clean_query = normalize_text(query)
        query_vector = self.model.encode(clean_query).tolist()
        semantic_hits = db_instance.search_vectors(query_vector=query_vector, limit=limit)
        keyword_hits = db_instance.keyword_search(query=clean_query, limit=limit)

        # id -> (score, payload); combine so keyword matches surface even when
        # the embedding score for short/single-word queries falls below min_score
        combined: dict = {}
        for hit in semantic_hits:
            if hit.score >= min_score:
                combined[hit.id] = (hit.score, hit.payload or {})

        query_lower = clean_query.lower()
        for point in keyword_hits:
            payload = point.payload or {}
            title = (payload.get("normalized_title") or payload.get("file_name") or "").lower()
            similarity = difflib.SequenceMatcher(None, query_lower, title).ratio()
            score = max(similarity, min_score)
            existing = combined.get(point.id)
            if not existing or score > existing[0]:
                combined[point.id] = (score, payload)

        results = []
        for point_id, (score, payload) in combined.items():
            jf_payload = payload.get("jellyfin") or {}
            meta_payload = payload.get("metadata") or {}

            file_path = payload.get("file_path") or jf_payload.get("path", "")
            file_name = payload.get("file_name") or (file_path.split("/")[-1] if file_path else "Unknown")
            mount = payload.get("mount") or payload.get("mount_name", "storage")

            media_type = payload.get("media_type") or ""
            audio = media_type in ("song", "audio", "music") or _is_audio(file_name)
            metadata = self._build_metadata(meta_payload, jf_payload, audio)

            results.append({
                "id": point_id,
                "score": round(score, 4),
                "file_name": file_name,
                "normalized_title": payload.get("normalized_title") or file_name,
                "file_path": file_path,
                "mount": mount,
                "media_type": media_type or ("song" if audio else "movie"),
                "folder_tags": _folder_tags(payload, file_path, audio),
                "width": metadata["width"],
                "height": metadata["height"],
                "resolution": metadata["resolution"],
                "quality": metadata["quality"],
                "primary_image_tag": meta_payload.get("primary_image_tag") or jf_payload.get("primary_image_tag"),
                "size": metadata["size"],
                "size_human": metadata["file_size_human"],
                "runtime_ticks": metadata["runtime_ticks"],
                "duration": metadata["duration"],
                "duration_formatted": metadata["duration_formatted"],
                "metadata": metadata,
                "jellyfin": jf_payload or {
                    "jf_id": payload.get("jellyfin_id"),
                    "jf_name": payload.get("title")
                }
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

search_engine = SemanticSearchEngine()