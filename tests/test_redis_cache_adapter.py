from bci_redis_cache import RedisCache


class RedisClient:
    def __init__(self):
        self.values = {}
        self.set_calls = []

    def set(self, key, value, **kwargs):
        self.set_calls.append((key, value, kwargs))
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def getset(self, key, value):
        prior = self.values.get(key)
        self.values[key] = value
        return prior


def test_zero_ttl_writes_persistent_value_without_redis_expiry():
    client = RedisClient()
    cache = RedisCache(redis_client=client)

    cache.set('persistent', {'value': 1}, ttl_seconds=0)

    assert client.set_calls[0][2] == {}
    assert cache.get('persistent') == {'value': 1}


def test_generation_activation_returns_prior_pointer():
    client = RedisClient()
    cache = RedisCache(redis_client=client)

    assert cache.activate_query_generation('srp', 'ppp', 'generation-1') is None
    assert cache.activate_query_generation('srp', 'ppp', 'generation-2') == 'generation-1'
    assert cache.get_query_generation('srp', 'ppp') == 'generation-2'
