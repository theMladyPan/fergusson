"""Builtin tools of the agent."""

from src.tools.bash import run_bash_command
from src.tools.fs import list_files, read_file_content, write_file_content


all_tools = [
    run_bash_command,
    list_files,
    read_file_content,
    write_file_content,
]
