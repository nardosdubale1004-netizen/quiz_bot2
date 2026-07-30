# src/db_diag.py
"""
Standalone network + DB diagnostic. Run manually inside the container to isolate
whether the ~500-1300ms per query is raw network RTT to Neon, TLS handshake overhead,
or connection pool contention. This bypasses the app entirely.

Usage (from inside the running container):
    docker exec -it quiz_bot2 python -m src.db_diag
"""
import socket
import ssl
import time
import statistics
import psycopg2
from urllib.parse import urlparse
from src.config import CONFIG

def parse_host_port(db_url: str):
    parsed = urlparse(db_url)
    return parsed.hostname, parsed.port or 5432

def raw_tcp_ping(host: str, port: int, attempts: int = 5):
    """Pure TCP connect time -- no TLS, no auth, no query. This is the network floor."""
    times = []
    for i in range(attempts):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=10) as s:
                pass
            dt = (time.perf_counter() - t0) * 1000
            times.append(dt)
            print(f"  [TCP connect #{i+1}] {dt:.0f}ms")
        except Exception as e:
            print(f"  [TCP connect #{i+1}] FAILED: {e}")
    return times

def raw_query_ping(db_url: str, attempts: int = 5):
    """
    Opens a FRESH psycopg2 connection each time (worst case: no pooling) and times
    connect+auth vs a bare `SELECT 1` on an already-open connection.
    """
    connect_times = []
    query_times = []
    for i in range(attempts):
        t0 = time.perf_counter()
        conn = psycopg2.connect(db_url, connect_timeout=10)
        connect_dt = (time.perf_counter() - t0) * 1000
        connect_times.append(connect_dt)

        t1 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        query_dt = (time.perf_counter() - t1) * 1000
        query_times.append(query_dt)

        conn.close()
        print(f"  [fresh connect+auth #{i+1}] {connect_dt:.0f}ms | [SELECT 1 on open conn #{i+1}] {query_dt:.0f}ms")
    return connect_times, query_times

def pooled_query_ping(attempts: int = 5):
    """Uses the app's ACTUAL connection pool, exactly as production code does."""
    from src.database import GLOBAL_ENGINE
    times = []
    for i in range(attempts):
        t0 = time.perf_counter()
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        GLOBAL_ENGINE.release_connection(conn)
        dt = (time.perf_counter() - t0) * 1000
        times.append(dt)
        print(f"  [pooled checkout+SELECT 1+release #{i+1}] {dt:.0f}ms")
    return times

def summarize(label, values):
    if not values:
        print(f"{label}: no data")
        return
    print(f"{label}: min={min(values):.0f}ms max={max(values):.0f}ms avg={statistics.mean(values):.0f}ms")

if __name__ == "__main__":
    db_url = CONFIG.get("database_url")
    if not db_url:
        print("No DATABASE_URL configured. Set it in .env or config.json.")
        raise SystemExit(1)

    host, port = parse_host_port(db_url)
    print(f"\n=== Diagnosing DB latency: {host}:{port} ===\n")

    print("1) Raw TCP connect (network floor, no TLS/auth/query):")
    tcp_times = raw_tcp_ping(host, port)
    summarize("   TCP connect", tcp_times)

    print("\n2) Fresh psycopg2 connect+auth, then SELECT 1 on that open connection:")
    connect_times, query_times = raw_query_ping(db_url)
    summarize("   Fresh connect+auth", connect_times)
    summarize("   SELECT 1 (conn already open)", query_times)

    print("\n3) Using the app's actual pool (get_db_connection -> SELECT 1 -> release):")
    pooled_times = pooled_query_ping()
    summarize("   Pooled SELECT 1", pooled_times)

    print("\n=== Diagnosis ===")
    if tcp_times and statistics.mean(tcp_times) > 200:
        print("-> TCP connect alone is slow. This IS network/physical-distance latency to Neon.")
        print("   No amount of app code changes will fix this — the fix is either:")
        print("   a) move the Neon project to a region closer to where this container runs, or")
        print("   b) reduce the NUMBER of round trips per action (batching/caching), since each")
        print("      one pays this same floor cost.")
    elif query_times and statistics.mean(query_times) > 200:
        print("-> TCP is fast but SELECT 1 on an already-open connection is still slow.")
        print("   This points to Neon's serverless proxy/pooler adding overhead per statement,")
        print("   or you're on a paused/scaling compute tier. Check your Neon dashboard's")
        print("   connection string — make sure you're using the POOLED connection string")
        print("   (host usually contains '-pooler'), not the direct one, or vice versa depending")
        print("   on your Neon plan's recommendation.")
    else:
        print("-> Raw network and query pings look fine. If the app is still slow, the issue is")
        print("   likely connection pool contention (too many concurrent checkouts against")
        print("   maxconn=20) or something is holding connections open too long.")