import argparse
import json
import os
import sys
from typing import List, Optional
import ollama
import pandas as pd
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 1. Target Schema definition using Pydantic
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


# ---------------------------------------------------------------------------
# 2. Parsing Function using Local LLM (gemma4:e2b)
# ---------------------------------------------------------------------------
def process_title_with_gemma(title: str, model_name: str = "gemma4:e2b") -> dict:
    prompt = f"""
    Extract entity metadata from this YouTube video title string:
    "{title}"

    Rules:
    1. 'song_title': Primary track or song name.
    2. 'movie_or_album': The movie or album name if explicitly present.
    3. 'artists': Put ALL singers, actors, dancers, and composers into this single array. 
    4. CRITICAL: Do NOT place the movie/album title inside the 'artists' list.
    """

    try:
        response = ollama.chat(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            format=TrackMetadata.model_json_schema(),  # Native JSON Schema constraint
            options={"temperature": 0},  # Deterministic execution
        )

        # Parse and validate with Pydantic schema
        data = TrackMetadata.model_validate_json(response.message.content)
        return data.model_dump()

    except Exception as e:
        print(f"Error processing title '{title[:30]}...': {e}")
        return {"song_title": title, "movie_or_album": None, "artists": []}


# ---------------------------------------------------------------------------
# 3. File Processing and Markdown Export CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Process YouTube titles using local LLM (gemma4:e2b) and output to Markdown."
    )
    parser.add_argument(
        "input_file", help="Path to text file containing list of titles (one per line)"
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Path to output Markdown file (default: <inputfile_name>.md)",
    )
    parser.add_argument(
        "--model",
        default="gemma4:e2b",
        help="Ollama model to use (default: gemma4:e2b)",
    )

    args = parser.parse_args()

    if not os.path.exists(args.input_file):
        print(f"Error: Input file '{args.input_file}' not found.")
        sys.exit(1)

    # Determine default output file name if not provided
    if args.output:
        output_file = args.output
    else:
        base_name = os.path.splitext(args.input_file)[0]
        output_file = f"{base_name}.md"

    # Read input titles
    with open(args.input_file, "r", encoding="utf-8") as f:
        titles = [line.strip() for line in f if line.strip()]

    print(f"Loaded {len(titles)} titles from '{args.input_file}'.")
    print(f"Processing with Ollama model '{args.model}'...\n")

    results = []
    for idx, raw_title in enumerate(titles, 1):
        print(f"[{idx}/{len(titles)}] Processing: {raw_title[:50]}...")
        parsed = process_title_with_gemma(raw_title, model_name=args.model)

        # Format artists list into a single comma-separated string for display
        artists_str = (
            ", ".join(parsed["artists"]) if parsed["artists"] else "N/A"
        )
        movie_str = (
            parsed["movie_or_album"] if parsed["movie_or_album"] else "N/A"
        )
        record = {
            "Song Title": parsed["song_title"],
            "Movie / Album": movie_str,
            "Artists": artists_str,
        }
        print(f"Record for '{raw_title}': {record}")
        results.append(
          record
        )

    # Create DataFrame and convert to Markdown Table
    df = pd.DataFrame(results)
    markdown_table = df.to_markdown(index=False)

    # Save to .md file
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# Processed Metadata (`{os.path.basename(args.input_file)}`)\n\n")
        f.write(f"**Total Items:** {len(titles)}\n\n")
        f.write(markdown_table)

    print(f"\nProcessing completed successfully!")
    print(f"Output saved to: {output_file}")


if __name__ == "__main__":
    main()