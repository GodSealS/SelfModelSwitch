from __future__ import annotations

import asyncio

import httpx
import pytest

from model_scheduler.contracts import Capability, Lease
from model_scheduler.gateway import DirectInferenceGateway


@pytest.mark.asyncio
async def test_gateway_uses_a_real_direct_loopback_socket() -> None:
    requests: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers = await reader.readuntil(b"\r\n\r\n")
        requests.append(headers)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{\"ok\":true}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(follow_redirects=False)
    try:
        gateway = DirectInferenceGateway({"chat": f"http://127.0.0.1:{port}"}, client)
        response = await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {"model": "chat", "messages": []}, asyncio.get_running_loop().time() + 2)
        assert await response.json() == {"ok": True}
        await response.aclose()
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
    assert b"POST /v1/chat/completions HTTP/1.1" in requests[0]


@pytest.mark.asyncio
async def test_gateway_preserves_sse_utf8_bytes_split_across_real_socket_writes() -> None:
    expected = b'data: {"delta":"' + "你好".encode() + b'"}\n\ndata: [DONE]\n\n'

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
        writer.write(expected[:18])
        await writer.drain()
        writer.write(expected[18:20])
        await writer.drain()
        writer.write(expected[20:])
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(follow_redirects=False)
    try:
        gateway = DirectInferenceGateway({"chat": f"http://127.0.0.1:{port}"}, client)
        response = await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {"model": "chat", "messages": [], "stream": True}, asyncio.get_running_loop().time() + 2)
        received = b"".join([chunk async for chunk in response.iter_bytes()])
        await response.aclose()
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
    assert received == expected
