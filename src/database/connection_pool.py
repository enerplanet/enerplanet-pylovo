"""
Database Connection Pool for PyLovo API.
Provides thread-safe connection pooling for better performance under load.
"""

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from src.config_loader import DBNAME, DBUSER, PASSWORD, HOST, PORT, TARGET_SCHEMA

# Global connection pool - initialized once at startup
_connection_pool = None

# Pool configuration
# 3 containers × 4 workers = 12 processes × 8 max = 96 connections (within PG's 200 limit)
MIN_CONNECTIONS = 2
MAX_CONNECTIONS = 8


def init_pool():
    """Initialize the connection pool. Call this at application startup."""
    global _connection_pool
    if _connection_pool is None:
        try:
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=MIN_CONNECTIONS,
                maxconn=MAX_CONNECTIONS,
                database=DBNAME,
                user=DBUSER,
                password=PASSWORD,
                host=HOST,
                port=PORT,
                options=f"-c search_path={TARGET_SCHEMA},public",
                connect_timeout=10
            )
            print(f"[Pool] Initialized connection pool: min={MIN_CONNECTIONS}, max={MAX_CONNECTIONS}")
        except Exception as e:
            print(f"[Pool] Failed to initialize pool: {e}")
            _connection_pool = None
            raise
    return _connection_pool


def get_pool():
    """Get the connection pool, initializing if needed."""
    global _connection_pool
    if _connection_pool is None:
        init_pool()
    return _connection_pool


def close_pool():
    """Close all connections in the pool. Call this at application shutdown."""
    global _connection_pool
    if _connection_pool is not None:
        _connection_pool.closeall()
        _connection_pool = None
        print("[Pool] Connection pool closed")


@contextmanager
def get_connection():
    """
    Context manager to get a connection from the pool.
    Automatically returns connection to pool when done.
    
    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...")
    """
    pool = get_pool()
    conn = None
    discard = False
    try:
        conn = pool.getconn()
        conn.rollback()
        yield conn
    finally:
        if conn is not None:
            try:
                if not conn.closed:
                    conn.rollback()
                else:
                    discard = True
            except Exception:
                discard = True
            pool.putconn(conn, close=discard)


@contextmanager
def get_cursor():
    """
    Context manager to get a cursor from a pooled connection.
    Handles connection and cursor lifecycle automatically.
    
    Usage:
        with get_cursor() as cur:
            cur.execute("SELECT ...")
            results = cur.fetchall()
    """
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
