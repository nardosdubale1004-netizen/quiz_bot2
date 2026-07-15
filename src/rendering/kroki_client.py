import os
import zlib
import base64
from src.config import Style

KROKI_ENDPOINT = os.getenv("KROKI_URL", "http://kroki:8000")

def get_latex_url(full_latex: str) -> str:
    """Compresses complete LaTeX documents and returns a valid Kroki endpoint URL."""
    compressed = zlib.compress(full_latex.encode('utf-8'), 9)
    encoded = base64.urlsafe_b64encode(compressed).decode('ascii')
    return f"{KROKI_ENDPOINT}/tikz/png/{encoded}"

async def fetch_kroki_image(client, img_url: str, full_latex_source: str = None):
    """Fetches PNG assets from the Kroki rendering container with persistent diagnostic logging."""
    try:
        resp = await client.get(img_url, timeout=25.0)
        if resp.status_code != 200:
            print(f"\n{Style.RED}[KROKI COMPILER EXCEPTION - STATUS {resp.status_code}]{Style.RESET}")
            print(f" ├─ Error Message: {resp.text[:500]}")
            if full_latex_source:
                log_path = "logs/failed_compilation.tex"
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(full_latex_source)
                print(f" └─ Failed LaTeX payload saved to: {log_path} for manual testing.")
        return resp
    except Exception as e:
        print(f"\n{Style.RED}[KROKI NETWORK EXCEPTION]{Style.RESET}: {e}")
        return None