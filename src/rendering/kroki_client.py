# src/rendering/kroki_client.py
import os
import zlib
import base64
from src.config import Style, CONFIG
from src.http_client import get_shared_client
from src.perf import timed

# Standardize endpoints with explicit fallbacks
KROKI_ENDPOINT = CONFIG.get("kroki_url") or "https://kroki.io"

def get_latex_url(full_latex: str) -> str:
    """Compresses complete LaTeX documents and returns a valid Kroki endpoint URL."""
    compressed = zlib.compress(full_latex.encode('utf-8'), 9)
    encoded = base64.urlsafe_b64encode(compressed).decode('ascii')
    return f"{KROKI_ENDPOINT}/tikz/png/{encoded}"

async def fetch_kroki_image(client=None, img_url: str = None, full_latex_source: str = None):
    """
    Fetches PNG assets from the Kroki rendering container with persistent diagnostic logging.

    NOTE: `client` is kept as a parameter purely for backward compatibility with existing
    call sites written as:
        async with httpx.AsyncClient() as client:
            resp = await fetch_kroki_image(client, img_url)
    Any client passed in is now ignored in favor of the shared, pooled client from
    src.http_client — this removes a fresh TCP+TLS handshake on every single diagram
    compile. Existing call sites in bot.py / callbacks.py / cli.py do not need to change.
    """
    shared = get_shared_client()
    try:
        with timed(f"Kroki fetch ({img_url[-30:] if img_url else 'unknown'})"):
            resp = await shared.get(img_url, timeout=15.0)
        if resp.status_code != 200:
            print(f"\n{Style.RED}[KROKI COMPILER EXCEPTION - STATUS {resp.status_code}]{Style.RESET}")
            print(f" ├─ Error Message: {resp.text[:500]}")
            if full_latex_source:
                log_path = "logs/failed_compilation.tex"
                os.makedirs("logs", exist_ok=True)
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(full_latex_source)
                print(f" └─ Failed LaTeX payload saved to: {log_path} for manual testing.")
        return resp
    except Exception as e:
        print(f"\n{Style.RED}[KROKI NETWORK EXCEPTION]{Style.RESET}: {e}")
        return None