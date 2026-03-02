"""Builtin tools of the agent."""

from src.tools.bash import run_bash_command
from src.tools.fs import (
    list_files,
    read_file_content,
    read_file_content_with_line_numbers,
    write_file_content,
    read_file_segment,
    replace_file_segment,
)
from src.tools.web_tools import get_content_from_url


all_tools = [
    run_bash_command,
    list_files,
    read_file_content,
    read_file_content_with_line_numbers,
    write_file_content,
    read_file_segment,
    replace_file_segment,
    get_content_from_url,
]
