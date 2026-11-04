"""MCP stdio smoke test against the real live library.

Skipped automatically when the Goodnotes container is absent. It never prints
page text or OCR content; images stay in memory.
"""
import asyncio
import base64
import io
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

LIVE = Path.home() / "Library/Containers/com.goodnotesapp.x/Data/Library/Databases"
EXPECTED_TOOLS = {
    "list_notebooks", "find_notebooks", "get_notebook_info",
    "get_page_image", "get_pages", "get_page_text", "get_page_text_hint",
    "server_status",
}


def _text(result: types.CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


@pytest.mark.skipif(not (LIVE / "projection.sqlite").exists(),
                    reason="live Goodnotes container not present")
def test_stdio_smoke():
    import gnstore
    nbs = gnstore.list_notebooks()
    assert nbs
    nb = next(n for n in nbs if n.get("page_count", 0) > 0)

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "goodnotes_inkeye_mcp.server"],
            env={"PYTHONUNBUFFERED": "1"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS

                r = await session.call_tool("list_notebooks", {})
                assert not r.isError
                text = _text(r)
                assert "source=live" in text and "notebooks" in text

                r = await session.call_tool("get_page_image",
                                            {"notebook": nb["id"], "page": 1})
                assert not r.isError
                label = _text(r)
                assert "warnings:" in label and "snapshot taken" in label
                images = [c for c in r.content if c.type == "image"]
                assert images
                im = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
                assert im.width > 0 and im.height > 0

                r = await session.call_tool("get_notebook_info",
                                            {"notebook": nb["id"]})
                assert not r.isError and "pages:" in _text(r)

                r = await session.call_tool("find_notebooks",
                                            {"query": nb["name"].split()[0][:20]})
                assert not r.isError

                r = await session.call_tool("get_pages",
                                            {"notebook": nb["id"], "start": 1,
                                             "end": 2, "max_side": 320})
                assert not r.isError
                assert len([c for c in r.content if c.type == "image"]) >= 1

                r = await session.call_tool("get_page_text",
                                            {"notebook": nb["id"], "page": 1})
                assert not r.isError
                import json
                payload = json.loads(_text(r))
                assert "typed_items" in payload and "ocr_hint" in payload

                r = await session.call_tool("get_page_text_hint",
                                            {"notebook": nb["id"], "page": 1})
                assert not r.isError and "UNRELIABLE" in _text(r)

                r = await session.call_tool("server_status", {})
                assert not r.isError
                status = _text(r)
                assert "goodnotes-inkeye" in status
                assert "gnreader:" in status and "capabilities_source:" in status

    asyncio.run(asyncio.wait_for(main(), timeout=240))
