from pathlib import Path

from pydantic_ai import ModelRetry

WORKSPACE_ROOT = Path("workspace").absolute()


def _check_path(path_str: str) -> Path:
    """Ensure the path is within the project root or workspace for safety."""

    path = Path(path_str).absolute()
    # For this personal assistant, we allow access to the project root and workspace
    project_root = Path(".").absolute()
    if not (path.is_relative_to(project_root) or path.is_relative_to(WORKSPACE_ROOT)):
        raise ValueError(f"Access denied: {path_str} is outside of the project scope.")

    return path


async def list_files(directory: str = "workspace") -> list[str]:
    """Lists files in a directory."""

    path = _check_path(directory)
    if not path.exists():
        raise ModelRetry(f"Directory {directory} does not exist.")

    return [str(f.relative_to(Path(".").absolute())) for f in path.iterdir()]


async def read_file_content(file_path: str) -> str:
    """Reads the content of a file."""

    path = _check_path(file_path)
    if not path.is_file():
        raise ModelRetry(f"{file_path} is not a file or does not exist.")

    return path.read_text(encoding="utf-8")


async def write_file_content(file_path: str, content: str) -> str:
    """Writes content to a file. Overwrites if exists."""

    try:
        path = _check_path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        return f"Successfully wrote to {file_path}"

    except Exception as e:
        raise ModelRetry(f"Failed to write to {file_path}: {str(e)}")


async def append_file_content(file_path: str, content: str) -> str:
    """Appends content to a file. Creates the file if it does not exist."""

    try:
        path = _check_path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

        return f"Successfully appended to {file_path}"

    except Exception as e:
        raise ModelRetry(f"Failed to append to {file_path}: {str(e)}")
