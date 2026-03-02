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


async def read_file_segment(
    file_path: str, start_line: int, end_line: int | None = None, elevated_privileges: bool = False
) -> str:
    """Reads a specific range of lines from a file (1-based indexing).

    Args:
        file_path (str): The path to the file.
        start_line (int): The starting line number (1-based).
        end_line (int | None): The ending line number (inclusive). If None, reads to the end.
        elevated_privileges (bool): Whether to bypass path restrictions.

    Returns:
        str: The content of the specified lines based on 1-based indexing.
    """

    path = _check_path(file_path, elevated_privileges)
    if not path.is_file():
        raise ModelRetry(f"{file_path} is not a file or does not exist.")

    try:
        content = path.read_text(encoding="utf-8")
        all_lines = content.splitlines(keepends=True)

        if not all_lines:
            return ""

        # Convert 1-based to 0-based
        start_idx = max(0, start_line - 1)

        # If start is beyond file length, return empty
        if start_idx >= len(all_lines):
            return ""

        # Calculate end slice index (exclusive)
        # end_line is inclusive 1-based index
        if end_line is None:
            end_idx = len(all_lines)
        else:
            end_idx = min(len(all_lines), end_line)

        # If range is invalid (start after end), return empty
        if start_idx >= end_idx:
            return ""

        segment = "".join(all_lines[start_idx:end_idx])
        return segment

    except Exception as e:
        raise ModelRetry(f"Failed to read segment from {file_path}: {str(e)}")


async def replace_file_segment(
    file_path: str, content: str, start_line: int, end_line: int, elevated_privileges: bool = False
) -> str:
    """Replaces a specific range of lines in a file with new content.

    Args:
        file_path (str): The path to the file.
        content (str): The new content to insert.
        start_line (int): The starting line number to replace (1-based).
        end_line (int): The ending line number to replace (inclusive).
        elevated_privileges (bool): Whether to bypass path restrictions.

    Returns:
        str: Status message indicating success.
    """

    path = _check_path(file_path, elevated_privileges)
    if not path.is_file():
        raise ModelRetry(f"{file_path} is not a file or does not exist.")

    try:
        current_content = path.read_text(encoding="utf-8")
        all_lines = current_content.splitlines(keepends=True)

        # Convert 1-based to 0-based
        start_idx = max(0, start_line - 1)

        # Calculate slicing end index.
        # end_line is inclusive. Slice end is exclusive.
        # But we must treat end_line relative to existing file lines.
        # If end_line > len(all_lines), just go to end.
        end_idx = min(len(all_lines), end_line)

        # Adjust indices if out of bounds
        if start_idx > len(all_lines):
            start_idx = len(all_lines)
            end_idx = len(all_lines)

        # Prepare content. Ensure it has a trailing newline if it's meant to be lines.
        # However, users might want to replace WITHOUT trailing newline.
        # We will trust the user provided 'content' largely, but standard behavior usually implies lines.
        # Let's ensure if content is not empty and surrounding lines exist, we manage transitions.

        # Actually, splitlines(keepends=True) keeps the \n of existing lines.
        # So we just slice and insert.

        new_lines_list = all_lines[:start_idx] + [content] + all_lines[end_idx:]

        # But wait, `content` is a string, not a list of lines.
        # If we list-ify it, `join` works if everything in list is string.
        # `splitlines` returns list of strings.
        # so `[content]` is list containing one string.
        # `"".join(...)` will work.

        # Does `content` need a newline at the end?
        # If it replaces L2..L3, and L4 follows, we want L1 + Content + L4.
        # If Content doesn't end with \n, L4 will continue on same line as Content.
        # This is usually NOT desired for "replace lines".
        # So check if we are inserting before existing lines.
        if end_idx < len(all_lines) and content and not content.endswith("\n"):
            content += "\n"

        new_lines_list = all_lines[:start_idx] + [content] + all_lines[end_idx:]
        new_content = "".join(new_lines_list)

        path.write_text(new_content, encoding="utf-8")

        return f"Successfully replaced lines {start_line} to {end_line} in {file_path}"

    except Exception as e:
        raise ModelRetry(f"Failed to replace segment in {file_path}: {str(e)}")
