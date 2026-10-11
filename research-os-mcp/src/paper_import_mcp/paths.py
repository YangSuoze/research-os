import re
from pathlib import Path

from paper_import_mcp.config import settings

PROJECTS_DIR = settings.workspace_root / "projects"
PAPERS_DIR = settings.workspace_root / "papers"
PAPER_IMPORTS_DIR = settings.workspace_root / "paper_imports"


def _path_part(value: str) -> str:
    part = value.replace(":", "_").replace("+", "_")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part):
        raise ValueError(f"Invalid path component: {value!r}")
    return part


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def project_db_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.db"


def project_repo_path(project_id: str) -> Path:
    return project_dir(project_id) / "repo"


def project_artifacts_path(project_id: str) -> Path:
    return project_dir(project_id) / "artifacts"


def paper_import_db_path() -> Path:
    return PAPER_IMPORTS_DIR / "jobs.sqlite"


def paper_import_temp_dir(batch_id: str) -> Path:
    return PAPER_IMPORTS_DIR / "tmp" / _path_part(batch_id)


def paper_dir(paper_id: str) -> Path:
    return PAPERS_DIR / _path_part(paper_id)


def paper_source_path(paper_id: str) -> Path:
    return paper_dir(paper_id) / "source.pdf"


def paper_mineru_dir(paper_id: str, mineru_version: str) -> Path:
    return paper_dir(paper_id) / "mineru" / _path_part(mineru_version)


def paper_graph_draft_path(paper_id: str, parse_version: str) -> Path:
    return paper_dir(paper_id) / "graph" / f"{_path_part(parse_version)}.json"
