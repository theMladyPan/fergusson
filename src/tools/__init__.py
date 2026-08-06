"""Builtin tools of the agent."""

from src.tools.audio import synthesize_speech, transcribe_audio
from src.tools.bash import run_bash_command
from src.tools.exa import web_search
from src.tools.fs import (
    list_files,
    read_file_content,
    read_file_content_with_line_numbers,
    write_file_content,
    read_file_segment,
    replace_file_segment,
)
from src.tools.gws import (
    create_calendar_event,
    get_contact,
    list_inbox_emails,
    list_upcoming_events,
    search_drive_docs,
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
    web_search,
    transcribe_audio,
    synthesize_speech,
    # Google Workspace native tools (read tools also on the router).
    list_inbox_emails,
    get_contact,
    list_upcoming_events,
    search_drive_docs,
    create_calendar_event,
]
