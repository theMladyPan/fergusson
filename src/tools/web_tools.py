"""Web tools such as web search, get raw content, etc."""

import asyncio
import os
import tempfile

import httpx
import logfire
from markitdown import MarkItDown
from pydantic_ai import ModelRetry

# Maximum content size (e.g., 10MB)
MAX_CONTENT_SIZE = 10 * 1024 * 1024


async def get_content_from_url(url: str) -> str:
    """
    Get markdown content from a URL.
    This tool performs a HEAD request to check content size, then downloads
    and converts the content to markdown using markitdown.

    Args:
        url: The URL to fetch content from.

    Returns:
        The content converted to markdown.
    """
    with logfire.span("get_content_from_url", url=url) as span:
        try:
            async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=30.0) as client:
                # HEAD request to check size
                try:
                    head_response = await client.head(url)
                    # Some servers return 405 for HEAD, which we can ignore and try GET
                    if head_response.status_code != 405:
                        head_response.raise_for_status()

                        content_length = head_response.headers.get("content-length")
                        if content_length and int(content_length) > MAX_CONTENT_SIZE:
                            raise ModelRetry(
                                f"Content too large ({content_length} bytes). Limit is {MAX_CONTENT_SIZE} bytes."
                            )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 405:
                        return f"Error checking URL status: {e}"

                except httpx.RequestError as e:
                    return f"Error checking URL: {e}"

                # Download content
                try:
                    response = await client.get(url)
                    response.raise_for_status()

                    content_len = len(response.content)
                    # Double check size if content-length was missing in HEAD
                    if content_len > MAX_CONTENT_SIZE:
                        return f"Error: Content too large ({content_len} bytes) after download. Limit is {MAX_CONTENT_SIZE} bytes."

                    # Create a temporary file to save the content
                    # MarkItDown typically works with file paths

                    # Determine extension from url or content-type
                    filename = url.split("/")[-1].split("?")[0]
                    if not filename:
                        filename = "downloaded_content"

                    suffix = os.path.splitext(filename)[1]
                    if not suffix:
                        # Try to guess from content-type
                        ct = response.headers.get("content-type", "").lower()
                        if "html" in ct:
                            suffix = ".html"
                        elif "pdf" in ct:
                            suffix = ".pdf"
                        elif "json" in ct:
                            suffix = ".json"
                        elif "xml" in ct:
                            suffix = ".xml"
                        elif "text" in ct:
                            suffix = ".txt"
                        else:
                            suffix = ".html"  # Default to HTML for web pages if unknown

                    # Write to temp file
                    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                    with os.fdopen(fd, "wb") as tmp_file:
                        tmp_file.write(response.content)

                    try:
                        with logfire.span("markitdown_conversion", url=url) as md_span:
                            # MarkItDown might be synchronous, wrap in thread if needed but for now direct call
                            # MarkItDown.convert() is synchronous. We should run it in executor.

                            def convert_sync(path):
                                md = MarkItDown()
                                result = md.convert(path)
                                return result.text_content

                            loop = asyncio.get_running_loop()
                            text_content = await loop.run_in_executor(None, convert_sync, tmp_path)

                            return text_content

                    except Exception as e:
                        raise ModelRetry(f"Error converting content to markdown: {e}")

                    finally:
                        # Clean up temp file
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

                except httpx.RequestError as e:
                    raise ModelRetry(f"Error fetching URL: {e}")

                except httpx.HTTPStatusError as e:
                    raise ModelRetry(f"HTTP error fetching URL: {e}")

                except Exception as e:
                    raise ModelRetry(f"Unexpected error fetching URL: {e}")

        except Exception as e:
            raise ModelRetry(f"Failed to fetch content from URL: {e}")
