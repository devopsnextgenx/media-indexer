import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from media_indexer.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-endpoint Ollama pool
# ---------------------------------------------------------------------------
@dataclass
class _EndpointState:
    endpoint: str
    healthy: bool = True
    last_checked: float = 0.0
    next_retry_at: float = 0.0
    last_error: Optional[str] = None


class OllamaServerPool:
    """Tracks a set of Ollama base URLs and hands out the first healthy one.

    Health is determined by a lightweight `GET /api/tags` probe. A server
    that fails is marked unhealthy and skipped for `unhealthy_backoff_seconds`
    before being retried, so a single flaky machine doesn't get hammered.
    When *every* endpoint is down, `wait_for_available()` blocks (sleeping
    `retry_wait_seconds` between sweeps) until one comes back, which is what
    gives callers pause-on-outage / resume-on-recovery behavior for free.
    """

    def __init__(
        self,
        endpoints: list[str],
        unhealthy_backoff_seconds: int = 30,
        retry_wait_seconds: int = 15,
        probe_timeout: float = 3.0,
    ):
        endpoints = [e.rstrip("/") for e in endpoints if e]
        if not endpoints:
            raise ValueError("OllamaServerPool requires at least one endpoint")
        self._lock = threading.Lock()
        self._states: dict[str, _EndpointState] = {e: _EndpointState(e) for e in endpoints}
        self._order = list(self._states.keys())
        self._rr_index = 0
        self.unhealthy_backoff_seconds = unhealthy_backoff_seconds
        self.retry_wait_seconds = retry_wait_seconds
        self.probe_timeout = probe_timeout

    # -- health probing ------------------------------------------------
    def _probe(self, endpoint: str) -> bool:
        try:
            res = requests.get(f"{endpoint}/api/tags", timeout=self.probe_timeout)
            return res.status_code == 200
        except Exception as e:
            logger.debug(f"Ollama probe failed for {endpoint}: {e}")
            return False

    def mark_unhealthy(self, endpoint: str, error: str = ""):
        with self._lock:
            st = self._states.get(endpoint)
            if not st:
                return
            st.healthy = False
            st.last_error = error
            st.last_checked = time.time()
            st.next_retry_at = st.last_checked + self.unhealthy_backoff_seconds
        logger.warning(f"Ollama endpoint {endpoint} marked unhealthy: {error}")

    def mark_healthy(self, endpoint: str):
        with self._lock:
            st = self._states.get(endpoint)
            if not st:
                return
            st.healthy = True
            st.last_error = None
            st.last_checked = time.time()
            st.next_retry_at = 0.0

    # -- selection --------------------------------------------------------
    def get_available_endpoint(self) -> Optional[str]:
        """Returns a healthy endpoint (round-robin among healthy ones),
        re-probing any endpoint whose backoff window has elapsed. Returns
        None if nothing is reachable right now."""
        now = time.time()
        with self._lock:
            order = self._order[self._rr_index:] + self._order[: self._rr_index]

        for endpoint in order:
            st = self._states[endpoint]
            if not st.healthy and now < st.next_retry_at:
                continue
            if not st.healthy and now >= st.next_retry_at:
                # Backoff elapsed — re-probe before handing it out again.
                if self._probe(endpoint):
                    self.mark_healthy(endpoint)
                else:
                    with self._lock:
                        st.next_retry_at = now + self.unhealthy_backoff_seconds
                    continue
            with self._lock:
                self._rr_index = (self._order.index(endpoint) + 1) % len(self._order)
            return endpoint
        return None

    def wait_for_available(
        self,
        should_stop: Optional[Callable[[], bool]] = None,
        on_waiting: Optional[Callable[[], None]] = None,
    ) -> Optional[str]:
        """Blocks until an endpoint is available, sleeping between sweeps.
        Returns the endpoint, or None if `should_stop()` returned True first
        (used so a paused/cancelled background job can exit the wait)."""
        while True:
            endpoint = self.get_available_endpoint()
            if endpoint:
                return endpoint
            if should_stop and should_stop():
                return None
            if on_waiting:
                on_waiting()
            time.sleep(self.retry_wait_seconds)

    def status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "endpoint": e,
                    "healthy": st.healthy,
                    "last_checked": st.last_checked,
                    "next_retry_at": st.next_retry_at,
                    "last_error": st.last_error,
                }
                for e, st in self._states.items()
            ]

    def refresh_all(self) -> list[dict]:
        """Actively re-probes every endpoint (used by the admin dashboard)."""
        for endpoint in self._order:
            if self._probe(endpoint):
                self.mark_healthy(endpoint)
            else:
                self.mark_unhealthy(endpoint, "probe failed")
        return self.status()


def _build_pool(cfg, backoff_attr="unhealthy_backoff_seconds", wait_attr="retry_wait_seconds") -> Optional[OllamaServerPool]:
    endpoints = cfg.all_endpoints() if hasattr(cfg, "all_endpoints") else [cfg.host]
    if not endpoints:
        return None
    return OllamaServerPool(
        endpoints,
        unhealthy_backoff_seconds=getattr(cfg, backoff_attr, 30),
        retry_wait_seconds=getattr(cfg, wait_attr, 15),
    )


# Shared pools, built once from settings, reused by the API server, the
# standalone llm_parser CLI, and the admin dashboard status endpoint.
llm_pool: Optional[OllamaServerPool] = _build_pool(settings.llm)
embedding_pool: Optional[OllamaServerPool] = _build_pool(settings.embedding) if settings.embedding.endpoints else None


class OllamaEmbeddingClient:
    def __init__(self, base_url: str | None = None, model_name: str | None = None):
        self.base_url = (base_url or settings.embedding.host).rstrip("/")
        self.model_name = model_name or settings.embedding.model_name
        self.pool = embedding_pool

    def encode(self, text: str | list[str], batch_size: int | None = None, **kwargs) -> list[float] | list[list[float]]:
        inputs = [text] if isinstance(text, str) else text
        base = (self.pool.get_available_endpoint() if self.pool else None) or self.base_url
        url = f"{base}/api/embed"
        payload = {"model": self.model_name, "input": inputs}

        try:
            res = requests.post(url, json=payload, timeout=30)
            res.raise_for_status()
            if self.pool:
                self.pool.mark_healthy(base)
            embeddings = res.json().get("embeddings", [])
            return embeddings[0] if isinstance(text, str) else embeddings
        except Exception as e:
            logger.error(f"Ollama embedding request failed on {url}: {e}")
            if self.pool:
                self.pool.mark_unhealthy(base, str(e))
            raise


class OllamaLLMClient:
    """Ollama text-generation/chat client. When a server pool is configured
    (multiple endpoints in config), each call picks a healthy endpoint;
    failed calls mark that endpoint unhealthy so the next call fails over."""

    def __init__(self, base_url: str | None = None, model_name: str | None = None, pool: Optional[OllamaServerPool] = None):
        self.base_url = (base_url or settings.llm.host).rstrip("/")
        self.model_name = model_name or settings.llm.model_name
        self.pool = pool if pool is not None else llm_pool

    def generate(self, prompt: str, system_prompt: str | None = None) -> str:
        url_base = (self.pool.get_available_endpoint() if self.pool else None) or self.base_url
        url = f"{url_base}/api/generate"
        payload = {"model": self.model_name, "prompt": prompt, "stream": False}
        if system_prompt:
            payload["system"] = system_prompt

        try:
            res = requests.post(url, json=payload, timeout=60)
            res.raise_for_status()
            if self.pool:
                self.pool.mark_healthy(url_base)
            return res.json().get("response", "")
        except Exception as e:
            logger.error(f"Ollama LLM generation failed on {url}: {e}")
            if self.pool:
                self.pool.mark_unhealthy(url_base, str(e))
            raise

    def chat(self, messages: list[dict], format: dict | str | None = None,
             options: dict | None = None, timeout: float = 60.0) -> str:
        """Calls /api/chat, optionally constrained to a JSON schema via
        `format` (e.g. a pydantic `model_json_schema()` dict — the same
        structured-output mechanism `llm-based.py` used through the ollama
        python client). Returns the raw message content string."""
        url_base = (self.pool.get_available_endpoint() if self.pool else None) or self.base_url
        url = f"{url_base}/api/chat"
        payload = {"model": self.model_name, "messages": messages, "stream": False}
        if format is not None:
            payload["format"] = format
        if options:
            payload["options"] = options

        try:
            res = requests.post(url, json=payload, timeout=timeout)
            res.raise_for_status()
            if self.pool:
                self.pool.mark_healthy(url_base)
            return res.json().get("message", {}).get("content", "")
        except Exception as e:
            logger.error(f"Ollama chat request failed on {url}: {e}")
            if self.pool:
                self.pool.mark_unhealthy(url_base, str(e))
            raise

    def is_any_endpoint_available(self) -> bool:
        if self.pool:
            return self.pool.get_available_endpoint() is not None
        try:
            requests.get(f"{self.base_url}/api/tags", timeout=3)
            return True
        except Exception:
            return False

    def wait_until_available(self, should_stop: Optional[Callable[[], bool]] = None,
                              on_waiting: Optional[Callable[[], None]] = None) -> bool:
        """Blocks (pausing the caller) until some endpoint responds, or
        `should_stop()` returns True. Returns False if it gave up because of
        should_stop, True once a server is reachable."""
        if self.pool:
            return self.pool.wait_for_available(should_stop=should_stop, on_waiting=on_waiting) is not None
        while not self.is_any_endpoint_available():
            if should_stop and should_stop():
                return False
            if on_waiting:
                on_waiting()
            time.sleep(settings.llm.retry_wait_seconds)
        return True