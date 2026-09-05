"""LLM-based song/movie/artist metadata parser.

This replaces the ad-hoc `llm-based.py` script with a reusable module that:

  1. Looks up a file name in `llm_parsed_metadata` first — if it's already
     been parsed, that cached row is returned and no LLM call is made.
  2. Otherwise calls the configured Ollama model (via the multi-endpoint
     pool in `media_indexer.llms`) to extract structured metadata, then
     writes the result back to `llm_parsed_metadata` so every future
     lookup for that file name — from any mount, from the duplicate
     detector, or from another run of this script — is a cache hit.
  3. Pauses automatically (rather than erroring out) if no Ollama endpoint
     is reachable, and resumes as soon as one comes back.

It can be imported (`parse_and_cache_title(...)`) by the duplicate detector
or the background job runner, or run directly as a CLI to independently
backfill the cache for a list of titles or for whatever the database
reports as unparsed:

    python -m media_indexer.llm_parser --mount songs --limit 500
    python -m media_indexer.llm_parser --input titles.txt --output out.md
"""
import argparse
import json
import logging
import os
import sys
from typing import List, Optional

from pydantic import BaseModel, Field

from media_indexer.config import settings
from media_indexer.database import mysql_db_instance
from media_indexer.llms import OllamaLLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Target schema
# ---------------------------------------------------------------------------
class TrackMetadata(BaseModel):
    song_title: str = Field(
        description="Title of the song or track name stripped of quality noise"
    )
    movie_or_album: Optional[str] = Field(
        default=None,
        description="Name of the movie, film, or album if present. Set to null if non-existent or if it matches artist names.",
    )
    artists: List[str] = Field(
        default_factory=list,
        description="A single list containing all singers, actors, dancers, and music directors. Do NOT repeat the movie_or_album title here.",
    )


PROMPT_TEMPLATE = """Extract entity metadata from this YouTube video title string:
"{title}"

Rules:
1. 'song_title': Primary track or song name.
2. 'movie_or_album': The movie or album name if explicitly present.
3. 'artists': Put ALL singers, actors, dancers, and composers into this single array.
4. CRITICAL: Do NOT place the movie/album title inside the 'artists' list."""


class ParserPaused(Exception):
    """Raised internally when a caller-supplied should_stop() aborts a wait."""


class LLMParseFailed(Exception):
    """Raised when an Ollama endpoint answered but its response couldn't be
    turned into valid metadata (e.g. malformed JSON, schema mismatch) --
    a problem with this file's title, not with the endpoint."""


class LLMEndpointUnavailable(Exception):
    """Raised when the actual Ollama call (chat/generate) itself failed --
    a connectivity/HTTP problem with the endpoint that served (or tried to
    serve) this request, not a problem with the file's title. The pool in
    media_indexer.llms already marks that endpoint unhealthy internally;
    this just tells the caller the failure wasn't the file's fault, so it's
    worth retrying (ideally against a different endpoint) rather than
    counting it as a permanent per-file failure."""


# ---------------------------------------------------------------------------
# Core parse-with-cache
# ---------------------------------------------------------------------------
def parse_and_cache_title(
    file_name: str,
    full_path: str = "",
    client: Optional[OllamaLLMClient] = None,
    force_reparse: bool = False,
    on_waiting_for_ollama=None,
    should_stop=None,
) -> dict:
    """Returns a dict with song_title/movie_or_album/artists for `file_name`,
    reusing the cached row in `llm_parsed_metadata` whenever possible.

    If Ollama is unreachable, this call blocks (pausing) until an endpoint
    becomes available again, unless `should_stop` returns True first — in
    which case it raises `ParserPaused` so a background job can stop
    cleanly mid-batch instead of hanging forever.
    """
    if not force_reparse:
        cached = mysql_db_instance.get_llm_metadata(file_name)
        if cached:
            return {
                "song_title": cached.get("song_title"),
                "movie_or_album": cached.get("movie_or_album"),
                "artists": cached.get("artists", []),
                "cached": True,
            }

    client = client or OllamaLLMClient(model_name=settings.jobs.llm_parsing.model_name or settings.llm.model_name)

    became_available = client.wait_until_available(should_stop=should_stop, on_waiting=on_waiting_for_ollama)
    if not became_available:
        raise ParserPaused(f"Stopped waiting for Ollama while parsing '{file_name}'")

    title_for_prompt = os.path.splitext(os.path.basename(file_name))[0]
    prompt = PROMPT_TEMPLATE.format(title=title_for_prompt)

    try:
        content = client.chat(
            messages=[{"role": "user", "content": prompt}],
            format=TrackMetadata.model_json_schema(),
            options={"temperature": 0},
            timeout=60,
        )
    except Exception as e:
        # The Ollama call itself failed (connection error, timeout, 5xx...).
        # llms.py's pool already marked that endpoint unhealthy internally;
        # this isn't the file's fault, so let the caller retry it rather
        # than caching a failure or counting it as a permanent per-file
        # failure.
        raise LLMEndpointUnavailable(f"Ollama call failed while parsing '{file_name}': {e}") from e

    try:
        parsed = TrackMetadata.model_validate_json(content)
        result = parsed.model_dump()
    except Exception as e:
        logger.error(f"LLM parse failed for '{file_name[:60]}': {e}")
        # Do NOT cache a fallback record: get_files_needing_llm_parse only
        # checks whether a row exists, not whether it's good data, so
        # writing a placeholder here would permanently mark this file as
        # "parsed" and it would never be retried. Instead, propagate so the
        # caller can count it as a genuine failure and leave it unwritten
        # for a later incremental rerun to pick up again.
        raise LLMParseFailed(f"Could not parse metadata for '{file_name}': {e}") from e

    endpoint = client.pool.get_available_endpoint() if client.pool else client.base_url
    mysql_db_instance.upsert_llm_metadata(
        file_name=file_name,
        full_path=full_path,
        song_title=result["song_title"],
        movie_or_album=result.get("movie_or_album"),
        artists=result.get("artists", []),
        model_name=client.model_name,
        source_endpoint=endpoint,
    )
    result["cached"] = False
    return result


# ---------------------------------------------------------------------------
# Standalone CLI — can independently (re)load the database from a list of
# titles, or from whatever `media_files` reports as not-yet-parsed.
# ---------------------------------------------------------------------------
def _iter_titles_from_db(mount: Optional[str], limit: Optional[int]):
    batch_size = settings.jobs.llm_parsing.batch_size
    after = None
    fetched = 0
    while True:
        remaining = batch_size if limit is None else min(batch_size, limit - fetched)
        if remaining <= 0:
            return
        rows = mysql_db_instance.get_files_needing_llm_parse(mount=mount, limit=remaining, after_file_name=after)
        if not rows:
            return
        for row in rows:
            yield row["file_name"], row.get("file_path", "")
            fetched += 1
        after = rows[-1]["file_name"]


def main():
    parser = argparse.ArgumentParser(
        description="Parse song/movie/artist metadata with a local LLM, caching results in MySQL for reuse."
    )
    parser.add_argument("--input", help="Text file of titles (one per line) to parse")
    parser.add_argument("--output", help="Markdown output file (default: <input>.md); only used with --input")
    parser.add_argument("--mount", help="Only pull unparsed files from this mount (used without --input)")
    parser.add_argument("--limit", type=int, default=None, help="Max number of files to process")
    parser.add_argument("--force", action="store_true", help="Re-parse even if a cached row already exists")
    parser.add_argument("--model", default=None, help="Ollama model to use (default: config)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    client = OllamaLLMClient(model_name=args.model or settings.jobs.llm_parsing.model_name)

    def on_waiting():
        logger.warning("No Ollama endpoint reachable — pausing parse run until one recovers...")

    if args.input:
        if not os.path.exists(args.input):
            print(f"Error: Input file '{args.input}' not found.")
            sys.exit(1)
        with open(args.input, "r", encoding="utf-8") as f:
            titles = [line.strip() for line in f if line.strip()]
        source = ((t, "") for t in titles)
        total_hint = len(titles)
    else:
        source = _iter_titles_from_db(args.mount, args.limit)
        total_hint = mysql_db_instance.count_files_needing_llm_parse(args.mount)

    logger.info(f"Starting LLM parse run (~{total_hint} candidate items)")

    results = []
    processed = 0
    cached_hits = 0
    failed = 0
    for file_name, full_path in source:
        try:
            record = parse_and_cache_title(
                file_name, full_path, client=client, force_reparse=args.force, on_waiting_for_ollama=on_waiting
            )
        except ParserPaused:
            logger.error("No Ollama endpoint became available; stopping run.")
            break
        except LLMEndpointUnavailable as e:
            failed += 1
            logger.warning(f"{e} (endpoint issue, not this title -- rerun to retry)")
            continue
        except LLMParseFailed as e:
            failed += 1
            logger.warning(f"{e} (left uncached; rerun without --force, or with --force, to retry)")
            continue
        if record.get("cached"):
            cached_hits += 1
        artists_str = ", ".join(record["artists"]) if record.get("artists") else "N/A"
        movie_str = record.get("movie_or_album") or "N/A"
        results.append({"File": file_name, "Song Title": record["song_title"],
                         "Movie / Album": movie_str, "Artists": artists_str})
        processed += 1
        if processed % 25 == 0:
            logger.info(f"Processed {processed}/{total_hint} ({cached_hits} cache hits)")

    logger.info(f"Done: {processed} processed, {cached_hits} served from cache, "
                f"{processed - cached_hits} newly parsed by the LLM, {failed} failed.")

    if args.input:
        try:
            import pandas as pd
            df = pd.DataFrame(results)
            markdown_table = df.to_markdown(index=False)
        except ImportError:
            markdown_table = "\n".join(json.dumps(r) for r in results)

        output_file = args.output or f"{os.path.splitext(args.input)[0]}.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"# Parsed Metadata (`{os.path.basename(args.input)}`)\n\n")
            f.write(f"**Total Items:** {len(results)}\n\n")
            f.write(markdown_table)
        print(f"Output saved to: {output_file}")


if __name__ == "__main__":
    main()