import asyncio
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
import httpx

logger = logging.getLogger(__name__)

# Reports library load progress as (library_name, items_loaded, total_items, done)
LibraryProgress = Callable[[str, int, Optional[int], bool], Union[None, Awaitable[None]]]


async def _report(callback: Optional[LibraryProgress], *args) -> None:
    if not callback:
        return
    try:
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        logger.debug(f"Library progress callback failed: {e}")


def extract_search_candidates(filename: str) -> List[str]:
    """Generates candidate search strings ordered from cleanest to fallback."""
    stem = filename.rsplit(".", 1)[0]
    
    # Replace underscores and dots with spaces
    clean_stem = stem.replace(".", " ").replace("_", " ")
    
    # 1. Primary candidate: Extract text before first hyphen or parenthesis
    primary_title = re.split(r"[-(\[]", clean_stem)[0]
    
    # Strip common video/audio release noise words
    noise_patterns = r"\b(1080p|720p|4k|2160p|bluray|brrip|webrip|dvdrip|x264|x265|hevc|yify|hd|hindi|eng|subtitles|full movie|movie)\b"
    clean_primary = re.sub(noise_patterns, "", primary_title, flags=re.IGNORECASE)
    clean_primary = re.sub(r"\b(19|20)\d{2}\b", "", clean_primary)
    clean_primary = " ".join(clean_primary.split())

    # 2. Secondary candidate: Full cleaned string across all segments
    full_clean = re.sub(noise_patterns, "", clean_stem, flags=re.IGNORECASE)
    full_clean = re.sub(r"\b(19|20)\d{2}\b", "", full_clean)
    full_clean = " ".join(full_clean.split())

    candidates = []
    if clean_primary:
        candidates.append(clean_primary)
    if full_clean and full_clean not in candidates:
        candidates.append(full_clean)
    if stem not in candidates:
        candidates.append(stem)
        
    return candidates


def format_ticks(ticks: Optional[int]) -> str:
    """Converts Jellyfin RunTimeTicks (100ns units) to hh:mm:ss or mm:ss."""
    if not ticks:
        return "N/A"
    total_seconds = int(ticks / 10_000_000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# Jellyfin returns nothing when the requested item types don't exist in a library,
# so the query has to follow the view's CollectionType.
COLLECTION_ITEM_TYPES: Dict[str, str] = {
    "movies": "Movie,Video",
    "musicvideos": "MusicVideo,Video,Movie",
    "tvshows": "Episode,Video",
    "music": "Audio,MusicVideo",
    "boxsets": "Movie,Episode,Video",
    "homevideos": "Video,Photo",
    "mixed": "Movie,Episode,Audio,Video,MusicVideo",
}
DEFAULT_ITEM_TYPES = "Movie,Episode,Audio,Video,MusicVideo"


def item_types_for_collection(collection_type: Optional[str]) -> str:
    return COLLECTION_ITEM_TYPES.get((collection_type or "").lower(), DEFAULT_ITEM_TYPES)


def normalize_mount_path(raw_path: str) -> str:
    """Maps remote Jellyfin storage path to local mount directory."""
    if not raw_path:
        return ""
    return raw_path.replace("/media/data/storage/ShareMe/media", "/media/storage")


class JellyfinClient:

    def __init__(
        self,
        base_url: str = "http://192.168.12.111:8096",
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
        request_delay: float = 0.05,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.request_delay = request_delay
        self.headers = {
            "Authorization": f'MediaBrowser Client="MediaIndexer", Device="IndexerService", DeviceId="indexer-01", Version="1.0.0", Token="{api_key}"'
            if api_key
            else ""
        }
        # Maps lowercased file basename -> normalized item metadata
        self._library_cache: Dict[str, Dict[str, Any]] = {}
        self._cached_views: List[str] = []
        # Maps lowercased library name -> {lowercased basename -> metadata}
        self._caches_by_library: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._views_by_name: Dict[str, str] = {}
        # Maps lowercased library name -> Jellyfin CollectionType
        self._collection_types_by_name: Dict[str, str] = {}

    def _normalize_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        media_sources = item.get("MediaSources") or []
        raw_ticks = item.get("RunTimeTicks")
        raw_path = item.get("Path") or (
            media_sources[0].get("Path") if media_sources else ""
        )
        size = media_sources[0].get("Size") if media_sources else None

        return {
            "jellyfin_id": item.get("Id"),
            "title": item.get("Name"),
            "overview": item.get("Overview", ""),
            "genres": item.get("Genres", []),
            "path": normalize_mount_path(raw_path),
            "width": item.get("Width"),
            "height": item.get("Height"),
            "primary_image_tag": (item.get("ImageTags") or {}).get("Primary"),
            "size": size,
            "runtime_ticks": raw_ticks,
            "duration": format_ticks(raw_ticks),
        }

    async def get_user_views(self) -> List[Dict[str, str]]:
        """Returns [{'Id': ..., 'Name': ..., 'CollectionType': ...}] per library view."""
        if not self.user_id:
            logger.warning("Jellyfin user_id not configured; cannot list user views")
            return []

        url = f"{self.base_url}/UserViews"
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=30.0) as client:
                res = await client.get(url, params={"userId": self.user_id})
                res.raise_for_status()
                return [
                    {
                        "Id": item.get("Id"),
                        "Name": item.get("Name"),
                        "CollectionType": item.get("CollectionType") or "",
                    }
                    for item in res.json().get("Items", [])
                    if item.get("Id")
                ]
        except Exception as e:
            logger.warning(f"Failed to fetch Jellyfin user views: {e}")
            return []

    async def get_library_items(
        self,
        parent_id: str,
        limit: int = 1000,
        library_name: str = "",
        on_progress: Optional[LibraryProgress] = None,
        collection_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetches all items under a library view, paging through the result set."""
        url = f"{self.base_url}/Users/{self.user_id}/Items"
        fields = "Path,MediaSources,Width,Height,ImageTags,RunTimeTicks,Genres,Overview"
        include_item_types = item_types_for_collection(collection_type)
        items: List[Dict[str, Any]] = []
        start_index = 0

        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=60.0) as client:
                while True:
                    params = {
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": include_item_types,
                        "Limit": limit,
                        "StartIndex": start_index,
                        "Fields": fields,
                    }
                    res = await client.get(url, params=params)
                    res.raise_for_status()
                    body = res.json()
                    batch = body.get("Items", [])
                    if not batch:
                        break

                    items.extend(batch)
                    start_index += len(batch)

                    total = body.get("TotalRecordCount")
                    await _report(
                        on_progress,
                        library_name or parent_id,
                        len(items),
                        total if isinstance(total, int) else None,
                        False,
                    )

                    if isinstance(total, int) and start_index >= total:
                        break
                    # No reliable total: a short page means the last page.
                    if total is None and len(batch) < limit:
                        break
        except Exception as e:
            logger.warning(f"Failed to fetch Jellyfin items for parent {parent_id}: {e}")

        logger.info(
            f"Fetched {len(items)} Jellyfin items for library "
            f"'{library_name or parent_id}'"
        )
        await _report(on_progress, library_name or parent_id, len(items), len(items), True)

        return items

    async def build_library_cache(
        self, view_names: Optional[List[str]] = None, force: bool = False
    ) -> int:
        """Bulk-loads library items once so per-file lookups are local dict hits."""
        if self._library_cache and not force:
            return len(self._library_cache)

        views = await self.get_user_views()
        if view_names:
            wanted = {n.lower() for n in view_names}
            views = [v for v in views if (v.get("Name") or "").lower() in wanted]

        self._library_cache = {}
        self._cached_views = [v.get("Name", "") for v in views]

        for view in views:
            items = await self.get_library_items(
                view["Id"],
                library_name=view.get("Name") or "",
                collection_type=view.get("CollectionType"),
            )
            for item in items:
                if item.get("IsFolder"):
                    continue
                normalized = self._normalize_item(item)
                raw_path = item.get("Path") or ""
                if not raw_path:
                    continue
                key = os.path.basename(raw_path).lower()
                self._library_cache.setdefault(key, normalized)
            logger.info(
                f"Cached {len(items)} Jellyfin items from library '{view.get('Name')}'"
            )

        logger.info(f"Jellyfin library cache built with {len(self._library_cache)} files")
        return len(self._library_cache)

    def lookup_by_filename(self, file_name: str) -> Optional[Dict[str, Any]]:
        """Resolves metadata from the prefetched cache by exact file basename."""
        if not self._library_cache:
            return None
        return self._library_cache.get(os.path.basename(file_name).lower())

    async def _resolve_view_ids(self, force: bool = False) -> Dict[str, str]:
        """Maps lowercased library name -> view id."""
        if self._views_by_name and not force:
            return self._views_by_name

        views = await self.get_user_views()
        self._views_by_name = {
            (v.get("Name") or "").lower(): v["Id"] for v in views if v.get("Name")
        }
        self._collection_types_by_name = {
            (v.get("Name") or "").lower(): v.get("CollectionType") or ""
            for v in views
            if v.get("Name")
        }
        return self._views_by_name

    async def build_library_caches(
        self,
        library_names: List[str],
        force: bool = False,
        on_progress: Optional[LibraryProgress] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Bulk-loads the named libraries; returns {library: {basename: metadata}}."""
        views = await self._resolve_view_ids(force=force)
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}

        for name in library_names:
            key = name.lower()
            if force or key not in self._caches_by_library:
                view_id = views.get(key)
                if not view_id:
                    logger.warning(f"Jellyfin library '{name}' not found in user views")
                    self._caches_by_library[key] = {}
                    await _report(on_progress, name, 0, 0, True)
                else:
                    cache: Dict[str, Dict[str, Any]] = {}
                    items = await self.get_library_items(
                        view_id,
                        library_name=name,
                        on_progress=on_progress,
                        collection_type=self._collection_types_by_name.get(key),
                    )
                    for item in items:
                        if item.get("IsFolder"):
                            continue
                        raw_path = item.get("Path") or ""
                        if not raw_path:
                            continue
                        normalized = self._normalize_item(item)
                        normalized["library"] = name
                        cache.setdefault(os.path.basename(raw_path).lower(), normalized)
                    self._caches_by_library[key] = cache
                    logger.info(
                        f"Cached {len(cache)} files from Jellyfin library '{name}'"
                    )
            else:
                await _report(
                    on_progress,
                    name,
                    len(self._caches_by_library[key]),
                    len(self._caches_by_library[key]),
                    True,
                )
            result[name] = self._caches_by_library[key]

        return result

    async def get_item_image(
        self,
        item_id: str,
        tag: Optional[str] = None,
        image_type: str = "Primary",
        fill_width: int = 251,
        fill_height: int = 377,
        quality: int = 96,
    ) -> Optional[tuple[bytes, str]]:
        """Fetches an item image from Jellyfin; returns (bytes, content_type)."""
        if not item_id or not re.fullmatch(r"[A-Za-z0-9\-]+", item_id):
            return None
        if image_type not in ("Primary", "Backdrop", "Thumb", "Logo"):
            return None

        params: Dict[str, Any] = {
            "fillWidth": fill_width,
            "fillHeight": fill_height,
            "quality": quality,
        }
        if tag and re.fullmatch(r"[A-Za-z0-9\-]+", tag):
            params["tag"] = tag

        url = f"{self.base_url}/Items/{item_id}/Images/{image_type}"
        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
                res = await client.get(url, params=params)
                if res.status_code == 200 and res.content:
                    return res.content, res.headers.get("content-type", "image/jpeg")
                logger.warning(
                    f"Jellyfin image fetch failed for {item_id}: HTTP {res.status_code}"
                )
        except Exception as e:
            logger.warning(f"Failed to fetch Jellyfin image for {item_id}: {e}")
        return None

    def build_stream_url(self, item_id: str) -> Optional[str]:
        """Direct-play URL for an item; API key stays server-side."""
        if not item_id or not re.fullmatch(r"[A-Za-z0-9\-]+", item_id):
            return None
        return f"{self.base_url}/Videos/{item_id}/stream?static=true&ApiKey={self.api_key}"

    async def fetch_item_metadata(
        self, file_name: str, media_type: str = "movie"
    ) -> Dict[str, Any]:
        """Queries candidate items in Jellyfin and isolates exact file matches."""
        cached = self.lookup_by_filename(file_name)
        if cached:
            return cached

        if self.request_delay > 0:
            await asyncio.sleep(self.request_delay)

        search_terms = extract_search_candidates(file_name)
        fields = "Path,MediaSources,Width,Height,ImageTags,RunTimeTicks"

        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                search_url = f"{self.base_url}/Items"
                items = []

                # Try candidate search queries sequentially until items are returned
                for term in search_terms:
                    params = {
                        "SearchTerm": term,
                        "IncludeItemTypes": "Movie,Episode,Audio"
                        if media_type == "auto"
                        else ("Movie" if media_type == "movie" else "Audio"),
                        "Limit": 20,
                        "Recursive": "true",
                        "Fields": fields,
                    }
                    if self.user_id:
                        params["userId"] = self.user_id

                    res = await client.get(search_url, params=params)
                    if res.status_code == 200:
                        items = res.json().get("Items", [])
                        if items:
                            break

                if not items:
                    return {"title": search_terms[0], "raw_filename": file_name}

                # Client-Side Match: Look for exact filename match in Path or MediaSources
                selected_item = None
                for candidate in items:
                    cand_path = candidate.get("Path", "")
                    media_sources = candidate.get("MediaSources", [])
                    source_path = media_sources[0].get("Path", "") if media_sources else ""

                    if (cand_path and os.path.basename(cand_path) == file_name) or (
                        source_path and os.path.basename(source_path) == file_name
                    ):
                        selected_item = candidate
                        break

                if not selected_item:
                    selected_item = items[0]

                media_sources = selected_item.get("MediaSources", [])
                size = media_sources[0].get("Size") if media_sources else None
                raw_ticks = selected_item.get("RunTimeTicks")
                raw_path = selected_item.get("Path") or (
                    media_sources[0].get("Path") if media_sources else ""
                )

                return {
                    "jellyfin_id": selected_item.get("Id"),
                    "title": selected_item.get("Name", search_terms[0]),
                    "genres": selected_item.get("Genres", []),
                    "path": normalize_mount_path(raw_path),
                    "width": selected_item.get("Width"),
                    "height": selected_item.get("Height"),
                    "primary_image_tag": selected_item.get("ImageTags", {}).get("Primary"),
                    "size": size,
                    "runtime_ticks": raw_ticks,
                    "duration": format_ticks(raw_ticks),
                }
            except Exception as e:
                logger.warning(f"Failed to fetch Jellyfin metadata for {file_name}: {e}")

        return {"title": search_terms[0], "raw_filename": file_name}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    async def debug_main():
        base_url = os.getenv("JELLYFIN_URL", "http://192.168.12.111:8096")
        api_key = os.getenv("JELLYFIN_API_KEY", "2f74464824354b2195e85757d4aaa723")
        user_id = os.getenv("JELLYFIN_USER_ID", "3610636a4f02446bbaa335524466ba9e")

        print("=" * 60)
        print(f" Connecting to Jellyfin Server: {base_url}")
        print(f" API Key: {api_key[:6]}... | User ID: {user_id}")
        print("=" * 60)

        client = JellyfinClient(
            base_url=base_url,
            api_key=api_key,
            user_id=user_id,
            request_delay=0.05,
        )

        test_files = [
            "Rudraksh_HD_-_Sanjay_Dutt_-_Sunil_Shetty_-_Bipasha_Basu_-_Hindi_Full_Movie_-_With_Eng_Subtitles.mp4",
            "Billy.Madison.1995.BluRay.720p.x264.YIFY.mkv",
            "Dostana 2008 Hindi 720p BRRip CharmeLeon Silver RG.mkv",
        ]

        for file_name in test_files:
            print(f"\n[QUERYING]: {file_name}")
            result = await client.fetch_item_metadata(file_name=file_name, media_type="movie")
            print("[RESULT]:")
            print(json.dumps(result, indent=4))
            print("-" * 60)

    asyncio.run(debug_main())