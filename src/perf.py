# src/perf.py
import time
import functools
from contextlib import contextmanager
from src.config import Style

@contextmanager
def timed(label: str):
    """Wall-clock timer for any block: `with timed('label'): ...`"""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000
        color = Style.GREEN if dt_ms < 250 else (Style.YELLOW if dt_ms < 800 else Style.RED)
        print(f"{color}[PERF] {label}: {dt_ms:.0f}ms{Style.RESET}", flush=True)

def timed_async(label_fn=None):
    """Decorator for async functions. label_fn(*args, **kwargs) -> string, or None to use func name."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            label = label_fn(*args, **kwargs) if label_fn else fn.__name__
            t0 = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                dt_ms = (time.perf_counter() - t0) * 1000
                color = Style.GREEN if dt_ms < 250 else (Style.YELLOW if dt_ms < 800 else Style.RED)
                print(f"{color}[PERF] {label}: {dt_ms:.0f}ms{Style.RESET}", flush=True)
        return wrapper
    return deco

def timed_sync(label_fn=None):
    """
    Decorator for plain sync functions (the DB helper functions in database.py, which run
    inside asyncio.to_thread). Applying this directly at the function definition means
    EVERY call site everywhere in the app gets timed automatically — no need to hunt down
    and wrap each individual `asyncio.to_thread(db_xxx, ...)` call by hand.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            label = label_fn(*args, **kwargs) if label_fn else fn.__name__
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                dt_ms = (time.perf_counter() - t0) * 1000
                color = Style.GREEN if dt_ms < 100 else (Style.YELLOW if dt_ms < 400 else Style.RED)
                print(f"{color}[PERF-DB] {label}: {dt_ms:.0f}ms{Style.RESET}", flush=True)
        return wrapper
    return deco