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
    model_name: str = "nomic-embed-text"
    dimension: int = 768

class LLMConfig(BaseModel):
    provider: str = "ollama"
    host: str = "http://host.docker.internal:11434"
    model_name: str = "gemma4:e2b"

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

class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    mounts: MountsConfig = MountsConfig()
    vectordb: VectorDBConfig = VectorDBConfig()
    mysql: MySQLConfig = MySQLConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    llm: LLMConfig = LLMConfig()
    logging: LoggingConfig = LoggingConfig()
    jellyfin: JellyfinConfig = JellyfinConfig()
    downloads: DownloadsConfig = DownloadsConfig()
    indexing: IndexingConfig = IndexingConfig()

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