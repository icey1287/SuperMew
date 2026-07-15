import unittest

from backend.infra.cache import RedisCache


class RedisCacheTests(unittest.TestCase):
    def test_strict_delete_propagates_transport_failure(self):
        class BrokenClient:
            def delete(self, _key):
                raise ConnectionError("redis unavailable")

        cache = RedisCache()
        cache._client = BrokenClient()

        with self.assertRaises(ConnectionError):
            cache.delete_strict("parent_chunk:chunk-1")

    def test_best_effort_delete_remains_non_throwing(self):
        class BrokenClient:
            def delete(self, _key):
                raise ConnectionError("redis unavailable")

        cache = RedisCache()
        cache._client = BrokenClient()

        self.assertIsNone(cache.delete("ephemeral"))


if __name__ == "__main__":
    unittest.main()
