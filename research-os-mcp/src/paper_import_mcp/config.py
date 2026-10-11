"""MCP paper-import settings, compatible with existing BACKEND_ variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_WORKSPACE = _PROJECT_ROOT.parent / "data" / "workspace"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env", env_prefix="BACKEND_", extra="ignore"
    )

    workspace_root: Path = _DEFAULT_WORKSPACE
    log_level: str = "INFO"
    llm_base_url: str = ""
    llm_api_key: str | None = None
    model_name: str = ""
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "paper_chunks_v1"
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "12345678"
    graph_embedding_model: str = "BAAI/bge-small-en-v1.5"
    github_token: str | None = None
    paper_extract_concurrency: int = 5


settings = Settings()
settings.workspace_root = settings.workspace_root.resolve()
