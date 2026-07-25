# src/rendering/kroki_client.py
import os
import zlib
import base64
import httpx
from src.config import Style, CONFIG

# Standardize endpoints with explicit fallbacks
KROKI_ENDPOINT = CONFIG.get("kroki_url") or "https://kroki.io"

def get_latex_url(full_latex: str) -> str:
    """Compresses complete LaTeX documents and returns a valid Kroki endpoint URL."""
    compressed = zlib.compress(full_latex.encode('utf-8'), 9)
    encoded = base64.urlsafe_b64encode(compressed).decode('ascii')
    return f"{KROKI_ENDPOINT}/tikz/png/{encoded}"

async def fetch_kroki_image(client: httpx.AsyncClient, img_url: str, full_latex_source: str = None):
    """Fetches PNG assets from the Kroki rendering container with persistent diagnostic logging."""
    try:
        # Enforce connection and read timeout protections
        resp = await client.get(img_url, timeout=15.0)
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