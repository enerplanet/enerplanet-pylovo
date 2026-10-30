"""
Redis-based cache for PyLovo API.
Caches static data like transformer sizes, cable types, consumer categories.
Falls back to in-memory cache if Redis is unavailable.
"""

import os
import time
import json
from typing import Any, Optional, Callable
from functools import wraps
import threading

# Redis connection
_redis_client = None
_use_redis = False

# Fallback in-memory cache
_cache = {}
_cache_lock = threading.Lock()

# Default TTL: 1 hour (static data rarely changes)
DEFAULT_TTL = 3600


def init_redis():
    """Initialize Redis connection."""
    global _redis_client, _use_redis
    
    redis_host = os.environ.get('REDIS_HOST', 'localhost')
    redis_port = int(os.environ.get('REDIS_PORT', 6379))
    redis_password = os.environ.get('REDIS_PASSWORD') or None
    
    try:
        import redis
        _redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5
        )
        # Test connection
        _redis_client.ping()
        _use_redis = True
        print(f"[Cache] Connected to Redis at {redis_host}:{redis_port}")
    except Exception as e:
        print(f"[Cache] Redis not available ({e}), using in-memory cache")
        _use_redis = False


def close_redis():
    """Close Redis connection."""
    global _redis_client, _use_redis
    if _redis_client:
        try:
            _redis_client.close()
        except:
            pass
        _redis_client = None
        _use_redis = False


def get(key: str) -> Optional[Any]:
    """Get a value from cache if it exists and hasn't expired."""
    if _use_redis and _redis_client:
        try:
            value = _redis_client.get(f"pylovo:{key}")
            if value:
                return json.loads(value)
        except Exception as e:
            print(f"[Cache] Redis get error: {e}")
    
    # Fallback to in-memory
    with _cache_lock:
        if key in _cache:
            value, expiry = _cache[key]
            if expiry is None or time.time() < expiry:
                return value
            else:
                del _cache[key]
    return None


def set(key: str, value: Any, ttl: Optional[int] = DEFAULT_TTL):
    """Set a value in cache with optional TTL (seconds). None TTL = never expires."""
    if _use_redis and _redis_client:
        try:
            _redis_client.setex(f"pylovo:{key}", ttl or 86400, json.dumps(value))
            return
        except Exception as e:
            print(f"[Cache] Redis set error: {e}")
    
    # Fallback to in-memory
    with _cache_lock:
        expiry = time.time() + ttl if ttl else None
        _cache[key] = (value, expiry)


def delete(key: str):
    """Delete a key from cache."""
    if _use_redis and _redis_client:
        try:
            _redis_client.delete(f"pylovo:{key}")
        except:
            pass
    
    with _cache_lock:
        if key in _cache:
            del _cache[key]


def clear():
    """Clear all cached values."""
    if _use_redis and _redis_client:
        try:
            # Only clear pylovo keys
            keys = _redis_client.keys("pylovo:*")
            if keys:
                _redis_client.delete(*keys)
        except:
            pass
    
    with _cache_lock:
        _cache.clear()
    print("[Cache] Cache cleared")


def cached(key_prefix: str, ttl: int = DEFAULT_TTL):
    """
    Decorator to cache function results.
    
    Usage:
        @cached("transformer_sizes", ttl=3600)
        def get_transformer_sizes():
            # expensive database query
            return results
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create cache key from prefix and arguments
            cache_key = f"{key_prefix}:{hash((args, tuple(sorted(kwargs.items()))))}"
            
            # Try to get from cache
            result = get(cache_key)
            if result is not None:
                return result
            
            # Not in cache, call function
            result = func(*args, **kwargs)
            
            # Store in cache
            set(cache_key, result, ttl)
            
            return result
        return wrapper
    return decorator


def get_stats() -> dict:
    """Get cache statistics."""
    stats = {"type": "redis" if _use_redis else "memory"}
    
    if _use_redis and _redis_client:
        try:
            keys = _redis_client.keys("pylovo:*")
            stats["total_entries"] = len(keys)
            stats["active_entries"] = len(keys)
            return stats
        except:
            pass
    
    with _cache_lock:
        total = len(_cache)
        now = time.time()
        expired = sum(1 for _, (_, exp) in _cache.items() if exp and exp < now)
        stats["total_entries"] = total
        stats["expired_entries"] = expired
        stats["active_entries"] = total - expired
    
    return stats
