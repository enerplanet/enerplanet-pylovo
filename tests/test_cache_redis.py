"""Integration test for the Redis connection of src/cache.py.

It needs a Redis server that requires a password, reached through REDIS_HOST,
REDIS_PORT and REDIS_PASSWORD, and is skipped when REDIS_PASSWORD is not set.
Importing src also needs the DBNAME, DBUSER, PASSWORD, HOST, PORT and
TARGET_SCHEMA variables set, though no database is opened. It writes and
deletes one key. Run from the project root:

    PYTHONPATH=api python -m unittest tests.test_cache_redis -v
"""
import os
import unittest

from src import cache

KEY = "pylovo:test:redis-password"


@unittest.skipUnless(os.environ.get("REDIS_PASSWORD"), "REDIS_PASSWORD not set")
class RedisPasswordTest(unittest.TestCase):
    """The cache uses a password-protected Redis instead of falling back to memory."""

    def tearDown(self):
        cache.delete(KEY)
        cache.close_redis()

    def test_connects_with_the_password(self):
        cache.init_redis()
        self.assertEqual(cache.get_stats()["type"], "redis")

        cache.set(KEY, {"value": 1})
        self.assertEqual(cache.get(KEY), {"value": 1})


if __name__ == "__main__":
    unittest.main()
