import pytest
from pathlib import Path
from src.config import settings
from src.tools.fs import _check_path, list_files

# Simple test to verify the logic we added to fs.py

def test_check_path_reroutes_workspace():
    """Test that 'workspace/foo' reroutes to settings.workspace_folder / 'foo'."""
    path = _check_path("workspace/foo.txt")
    expected = (settings.workspace_folder / "foo.txt").absolute()
    assert path == expected

def test_check_path_reroutes_workspace_root():
    """Test that 'workspace' reroutes to settings.workspace_folder."""
    path = _check_path("workspace")
    expected = settings.workspace_folder.absolute()
    assert path == expected

@pytest.mark.asyncio
async def test_list_files_workspace():
    """Test listing files in workspace uses correct path."""
    # Ensure workspace dir exists
    settings.workspace_folder.mkdir(parents=True, exist_ok=True)
    
    files = await list_files("workspace")
    assert isinstance(files, list)
