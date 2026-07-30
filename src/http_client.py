# src/http_client.py
import httpx

_client: httpx.AsyncClient | None = None

def get_shared_client() -> httpx.AsyncClient:
    """
    Returns a single process-wide httpx client with connection pooling / keep-alive.
    Previously every send_rich_message_safe / edit_rich_message_safe / fetch_kroki_image
    call opened `async with httpx.AsyncClient() as client:` — a brand new TCP connection
    and TLS handshake EVERY single time. This shares one pooled client for the whole
    process so repeat calls to api.telegram.org and your Kroki host reuse warm
    connections instead of paying a fresh handshake each time.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=40, max_connections=100, keepalive_expiry=60.0),
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0),
        )
    return _client

async def close_shared_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None