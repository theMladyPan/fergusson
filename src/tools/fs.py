import os
from pathlib import Path
from typing import List
from pydantic_ai import RunContext

WORKSPACE_ROOT = Path("workspace").absolute()

def _check_path(path_str: str) -> Path:
    """Ensure the path is within the project root or workspace for safety."""
    path = Path(path_str).absolute()
    # For this personal assistant, we allow access to the project root and workspace
    project_root = Path(".").absolute()
    if not (path.is_relative_to(project_root) or path.is_relative_to(WORKSPACE_ROOT)):
        raise ValueError(f"Access denied: {path_str} is outside of the project scope.")
    return path

async def list_files(ctx: RunContext[None], directory: str = "workspace") -> List[str]:
    """Lists files in a directory."""
    path = _check_path(directory)
    if not path.exists():
        return [f"Directory {directory} does not exist."]
    return [str(f.relative_to(Path(".").absolute())) for f in path.iterdir()]

async def read_file_content(ctx: RunContext[None], file_path: str) -> str:
    """Reads the content of a file."""
    path = _check_path(file_path)
    if not path.is_file():
        return f"Error: {file_path} is not a file."
    return path.read_text(encoding="utf-8")

async def write_file_content(ctx: RunContext[None], file_path: str, content: str) -> str:
    """Writes content to a file. Overwrites if exists."""
    path = _check_path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Successfully wrote to {file_path}"
