import os
import logging
import yaml
from pydantic import BaseModel, Field

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 2345

class FolderLibraryMap(BaseModel):
    folder: str = ""
    libraries: list[str] = Field(default_factory=list)

class MountConfig(BaseModel):
    enabled: bool = True
    path: str
    media_type: str = "auto"
    folders: list[FolderLibraryMap] = Field(default_factory=list)

class MountsConfig(BaseModel):
    base_dir: str = "/media"
    registry: dict[str, MountConfig] = Field(default_factory=dict)

class VectorDBConfig(BaseModel):
    type: str = "qdrant"
    embedded_path: str = "/app/data/qdrant"
    collection_name: str = "media_library"
    host: str | None = None
    port: int | None = None

class MySQLConfig(BaseModel):
    enabled: bool = True
    host: str = "host.docker.internal"
    port: int = 3306
    user: str = "minis"
    password: str = "m1nIspsswd"
    database: str = "medialib"
    root_pwd: str = "p@ssw0rd"

class EmbeddingConfig(BaseModel):
    provider: str = "ollama"
    host: str = "http://host.docker.internal:11434"
    # Optional list of additional Ollama servers. When non-empty, `host` is
    # treated as just the first/default entry and all are pooled together.
    endpoints: list[str] = Field(default_factory=list)
    model_name: str = "nomic-embed-text"
    dimension: int = 768

    def all_endpoints(self) -> list[str]:
        eps = [self.host] + [e for e in self.endpoints if e != self.host]
        return [e.rstrip("/") for e in eps if e]

class LLMConfig(BaseModel):
    provider: str = "ollama"
    host: str = "http://host.docker.internal:11434"
    # List of Ollama server base URLs that can serve `generate`/`chat`/`embed`
    # for LLM parsing. Populate this to load-balance / failover across
    # multiple machines. `host` above is always included as the first entry.
    endpoints: list[str] = Field(default_factory=list)
    model_name: str = "gemma4:e2b"
    # How long (seconds) a server that failed a health check is skipped
    # before being retried.
    unhealthy_backoff_seconds: int = 30
    # How long to sleep between availability checks while every configured
    # endpoint is down (parsing pauses and resumes automatically).
    retry_wait_seconds: int = 15

    def all_endpoints(self) -> list[str]:
        eps = [self.host] + [e for e in self.endpoints if e != self.host]
        return [e.rstrip("/") for e in eps if e]

class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

class JellyfinConfig(BaseModel):
    enabled: bool = True
    url: str = ""
    api_key: str = ""
    user_id: str = ""

class DownloadsConfig(BaseModel):
    songs_root: str = "/media/storage/songs"
    movies_root: str = "/media/storage/movies"

class AutoScanConfig(BaseModel):
    enabled: bool = False
    cron: str = "0 3 * * *"
    incremental: bool = True

class IndexingConfig(BaseModel):
    workers: int = Field(default=4, ge=1, le=64)
    batch_size: int = Field(default=32, ge=1, le=512)
    log_buffer: int = Field(default=5000, ge=100, le=100000)
    auto_scan: AutoScanConfig = AutoScanConfig()

class RedisConfig(BaseModel):
    enabled: bool = True
    host: str = "host.docker.internal"
    port: int = 6379
    db: int = 0
    password: str | None = None

class DuplicatesConfig(BaseModel):
    enabled: bool = True
    similarity_threshold: float = 0.85
    # If true, scanning a mount no longer auto-triggers duplicate detection
    # inline; detection is a separate job triggered independently per mount
    # (via API or the background job scheduler).
    decoupled_from_indexing: bool = True
    # Use llm_parsed_metadata (song/movie/artist) when available instead of
    # the heuristic token classifier for a given file's tiers.
    use_llm_metadata: bool = True

class LLMParsingConfig(BaseModel):
    enabled: bool = True
    model_name: str = "gemma4:e2b"
    # Max titles processed per background-job "tick" before re-checking
    # resource availability / pause state.
    batch_size: int = 20

class ResourceGateConfig(BaseModel):
    """Simple, dependency-free heuristic for 'only run when resources are
    free' used by the background job scheduler."""
    enabled: bool = True
    max_load_average_1m: float = 15.0
    max_cpu_percent: float = 75.0
    check_interval_seconds: int = 5

class BackgroundJobsConfig(BaseModel):
    resource_gate: ResourceGateConfig = ResourceGateConfig()
    llm_parsing: LLMParsingConfig = LLMParsingConfig()

# Update AppConfig class
class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    mounts: MountsConfig = MountsConfig()
    vectordb: VectorDBConfig = VectorDBConfig()
    mysql: MySQLConfig = MySQLConfig()
    redis: RedisConfig = RedisConfig() # Added Redis config
    embedding: EmbeddingConfig = EmbeddingConfig()
    llm: LLMConfig = LLMConfig()
    logging: LoggingConfig = LoggingConfig()
    jellyfin: JellyfinConfig = JellyfinConfig()
    downloads: DownloadsConfig = DownloadsConfig()
    indexing: IndexingConfig = IndexingConfig()
    duplicates: DuplicatesConfig = DuplicatesConfig()
    jobs: BackgroundJobsConfig = BackgroundJobsConfig()


def load_config(config_path: str = "config.yml") -> AppConfig:
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            config = AppConfig(**data)
    else:
        config = AppConfig()

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format=config.logging.format,
    )
    return config

settings = load_config()