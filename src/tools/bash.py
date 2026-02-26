import asyncio
import shlex
from typing import Tuple
from pydantic_ai import RunContext
from loguru import logger

HAZARDOUS_PATTERNS = [
    "rm ", "sudo ", "mv ", "chmod ", "chown ", "mkfs ", "dd ",
    "> /dev/", ":(){ :|:& };:", "rmdir "
]

async def run_bash_command(ctx: RunContext[None], command: str) -> str:
    """
    Executes a bash command and returns the output (stdout/stderr).
    Hazardous commands (rm, sudo, etc.) will return a request for confirmation.
    
    Args:
        command: The shell command to execute.
    """
    # Guardrail: Check for hazardous commands
    is_hazardous = any(pattern in command for pattern in HAZARDOUS_PATTERNS)
    
    # In a real scenario, we'd check ctx.deps for a 'permission_granted' flag 
    # or look into the message history for a "YES" to this specific command.
    # For now, we instruct the agent to ask if it detects hazard.
    if is_hazardous:
        return (
            f"CRITICAL: The command '{command}' is marked as potentially hazardous. "
            "You MUST explicitly ask the user for permission before I can execute this."
        )

    logger.info(f"Executing bash command: {command}")
    
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()
    
    result = []
    if stdout:
        result.append(stdout.decode())
    if stderr:
        result.append(f"Errors:
{stderr.decode()}")
        
    return "
".join(result) if result else "Command executed successfully (no output)."
