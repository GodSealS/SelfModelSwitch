from __future__ import annotations

import pytest

from model_scheduler.llama_swap_client import LlamaSwapClient, LlamaSwapProtocolError


@pytest.mark.asyncio
async def test_running_refuses_to_guess_an_unpinned_llama_swap_schema() -> None:
    client = LlamaSwapClient("http://127.0.0.1:8080")
    with pytest.raises(LlamaSwapProtocolError, match="fixture"):
        await client.running()
