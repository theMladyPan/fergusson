from pathlib import Path

from pydantic_ai import ModelRetry

from src.config import settings


def _check_path(path_str: str, bypass: bool) -> Path:
    """Ensure the path is within the project root or workspace for safety."""

    # If path starts with "workspace/", reroute to settings.workspace_folder
    if path_str == "workspace" or path_str.startswith("workspace/"):
        rel = path_str.replace("workspace", "", 1).lstrip("/")
        path = (settings.workspace_folder / rel).absolute()
    else:
        path = Path(path_str).absolute()
    # For this personal assistant, we allow access to the project root and workspace
    project_root = Path(".").absolute()
    workspace_root = settings.workspace_folder.absolute()

    if bypass:
        return path

    # We allow paths that are relative to either project root OR workspace folder
    if not (path.is_relative_to(project_root) or path.is_relative_to(workspace_root)):
        raise ModelRetry(
            f"Access denied: {path_str} is outside of the project scope. You need to ask for user permission to access files outside of the workspace or project root."
        )

    return path


async def list_files(directory: str = "workspace", elevated_privileges: bool = False) -> list[str]:
    """Lists files in a directory. Prefer this tool over using bash commands for file management.

    Args:
        directory (str): The directory to list files from.
        elevated_privileges (bool): Whether to bypass path restrictions, default is False.

    Returns:
        list[str]: A list of file paths relative to the project root or workspace.
    """

    path = _check_path(directory, elevated_privileges)
    if not path.exists():
        raise ModelRetry(f"Directory {directory} does not exist.")

    return [str(f.relative_to(Path(".").absolute())) for f in path.iterdir()]


async def read_file_content(file_path: str, elevated_privileges: bool = False) -> str:
    """Reads the content of a file.

    Args:
        file_path (str): The path to the file to read.
        elevated_privileges (bool): Whether to bypass path restrictions, default is False.

    Returns:
        str: The content of the file.
    """

    path = _check_path(file_path, elevated_privileges)
    if not path.is_file():
        raise ModelRetry(f"{file_path} is not a file or does not exist.")

    return path.read_text(encoding="utf-8")


async def write_file_content(file_path: str, content: str, elevated_privileges: bool = False) -> str:
    """Writes content to a file. Overwrites if exists.

    Args:
        file_path (str): The path to the file to write.
        content (str): The content to write to the file.
        elevated_privileges (bool): Whether to bypass path restrictions, default is False.

    Returns:
        str: A message indicating the result of the write operation.
    """

    try:
        path = _check_path(file_path, elevated_privileges)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        return f"Successfully wrote to {file_path}"

    except Exception as e:
        raise ModelRetry(f"Failed to write to {file_path}: {str(e)}")


async def append_file_content(file_path: str, content: str, elevated_privileges: bool = False) -> str:
    """Appends content to a file. Creates the file if it does not exist.

    Args:
        file_path (str): The path to the file to append.
        content (str): The content to append to the file.
        elevated_privileges (bool): Whether to bypass path restrictions, default is False.

    Returns:
        str: A message indicating the result of the append operation.
    """

    try:
        path = _check_path(file_path, elevated_privileges)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

        return f"Successfully appended to {file_path}"

    except Exception as e:
        raise ModelRetry(f"Failed to append to {file_path}: {str(e)}")
