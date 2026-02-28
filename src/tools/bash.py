import asyncio
import shlex
from typing import Annotated, Tuple

import logfire
from pydantic_ai import ModelRetry

HAZARDOUS_PATTERNS = ["rm ", "sudo ", "mv ", "chmod ", "chown ", "mkfs ", "dd ", "> /dev/", ":(){ :|:& };:", "rmdir "]


async def run_bash_command(
    command: str,
    override: Annotated[bool, "Override hazardous command check"] = False,
) -> str:
    """
    Executes a bash command and returns the output (stdout/stderr).
    Hazardous commands (rm, sudo, etc.) will return a request for confirmation.

    Args:
        command: The shell command to execute.
        override: If True, bypasses the hazardous command check. Use only after user confirmation.

    Returns:
        The combined stdout and stderr from the command execution, or a confirmation request if the command is hazardous.

    Raises:
        ModelRetry: If the command is deemed hazardous and override is not set, prompting the agent to ask the user for permission.
    """
    # Guardrail: Check for hazardous commands
    is_hazardous = any(pattern in command for pattern in HAZARDOUS_PATTERNS)

    # In a real scenario, we'd check ctx.deps for a 'permission_granted' flag
    # or look into the message history for a "YES" to this specific command.
    # For now, we instruct the agent to ask if it detects hazard.
    if is_hazardous and not override:
        raise ModelRetry(
            f"CRITICAL: The command '{command}' is marked as potentially hazardous."
            "You MUST explicitly ask the user for permission before I can execute this."
        )

    logfire.info(f"Executing bash command: {command}")

    process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    result = []
    if stdout:
        result.append(stdout.decode())
    if stderr:
        result.append(f"Errors:\n{stderr.decode()}")

    return "\n".join(result) if result else "Command executed successfully (no output)."
