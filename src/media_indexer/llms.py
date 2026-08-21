import logging
import requests
from media_indexer.config import settings

logger = logging.getLogger(__name__)

class OllamaEmbeddingClient:
    def __init__(self, base_url: str | None = None, model_name: str | None = None):
        self.base_url = (base_url or settings.embedding.host).rstrip("/")
        self.model_name = model_name or settings.embedding.model_name

    def encode(self, text: str | list[str], batch_size: int | None = None, **kwargs) -> list[float] | list[list[float]]:
        inputs = [text] if isinstance(text, str) else text
        url = f"{self.base_url}/api/embed"
        payload = {"model": self.model_name, "input": inputs}
        
        try:
            res = requests.post(url, json=payload, timeout=30)
            res.raise_for_status()
            embeddings = res.json().get("embeddings", [])
            return embeddings[0] if isinstance(text, str) else embeddings
        except Exception as e:
            logger.error(f"Ollama embedding request failed on {url}: {e}")
            raise

class OllamaLLMClient:
    def __init__(self, base_url: str | None = None, model_name: str | None = None):
        self.base_url = (base_url or settings.llm.host).rstrip("/")
        self.model_name = model_name or settings.llm.model_name

    def generate(self, prompt: str, system_prompt: str | None = None) -> str:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False
        }
        if system_prompt:
            payload["system"] = system_prompt

        try:
            res = requests.post(url, json=payload, timeout=60)
            res.raise_for_status()
            return res.json().get("response", "")
        except Exception as e:
            logger.error(f"Ollama LLM generation failed on {url}: {e}")
            raise