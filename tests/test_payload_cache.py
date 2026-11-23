"""Synthetic-only tests of persistence, isolation and exact canonical rows."""
from contextlib import contextmanager
from datetime import datetime,timezone
from unittest.mock import MagicMock
import json
import pytest
import fakeredis
from app import payload_cache
from bci_redis_cache.cache import RedisCache


def record(second=1):
    return {'generation_epoch':float(second),'freshness':datetime.fromtimestamp(second,timezone.utc).isoformat(),
            'definition_hash':'definition','rows':[{'payload_json':{'amount':2,'missing':None}}]}


def test_atomic_persistent_replacement_and_late_completion():
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    assert cache.publish_load_bound_payload('synthetic',record(1))
    assert cache.redis.ttl('synthetic') == -1
    assert cache.get('synthetic') == record(1)
    assert cache.publish_load_bound_payload('synthetic',record(3))
    assert not cache.publish_load_bound_payload('synthetic',record(2))
    assert cache.get('synthetic') == record(3)
    assert list(cache.redis.scan_iter()) == ['synthetic']


def test_failed_write_preserves_last_good(monkeypatch):
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    cache.publish_load_bound_payload('synthetic',record(1))
    def unavailable(*args,**kwargs):raise ConnectionError('synthetic failure')
    monkeypatch.setattr(cache.redis,'eval',unavailable)
    with pytest.raises(ConnectionError):cache.publish_load_bound_payload('synthetic',record(2))
    assert cache.get('synthetic') == record(1)


def test_policy_is_explicit_and_safe_default():
    data=MagicMock();data.execute.return_value.fetchone.return_value=(None,)
    assert payload_cache.policy(data,'public.synthetic') is None
    assert data.execute.call_count == 1
    data.execute.return_value.fetchone.side_effect=[('function',),({'mode':'load_bound','scope':'identity'},'hash')]
    with pytest.raises(ValueError):payload_cache.policy(data,'public.synthetic')


def test_payload_key_separates_artifact_client_and_definition():
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    a=payload_cache.key(cache,'client','one','public.synthetic',{'definition_hash':'a'})
    assert a != payload_cache.key(cache,'other','one','public.synthetic',{'definition_hash':'a'})
    assert a != payload_cache.key(cache,'client','two','public.synthetic',{'definition_hash':'a'})
    assert a != payload_cache.key(cache,'client','one','public.synthetic',{'definition_hash':'b'})


def test_warm_lookup_never_reads_canonical_view(monkeypatch):
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    configuration={'definition_hash':'definition'}
    payload_cache.publish(cache,'client','report','public.synthetic',configuration,record())
    monkeypatch.setattr(payload_cache,'policy',lambda *args:configuration)
    data=MagicMock()
    assert payload_cache.lookup(data,cache,'client','report','public.synthetic')[1] == record()
    data.execute.assert_not_called()


def test_select_pins_freshness_and_rows_in_one_statement():
    data=MagicMock();data.execute.return_value.fetchone.return_value=('2026-11-06 01:00:00+00',record()['rows'])
    value=payload_cache.select_current(data,'client','report','public.synthetic',{'definition_hash':'definition'})
    assert value['rows']==record()['rows']
    assert data.execute.call_count==1
    assert 'jsonb_agg(to_jsonb(p))' in data.execute.call_args.args[0]
    assert 'bci_artifact_cache_freshness' in data.execute.call_args.args[0]


def test_prewarm_has_no_qc_render_or_delivery(monkeypatch):
    from app import engine,cache as module
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    @contextmanager
    def connection():yield MagicMock()
    monkeypatch.setattr(engine,'get_metadata_conn',connection)
    monkeypatch.setattr(engine,'get_data_conn',connection)
    monkeypatch.setattr(engine,'_fetch_artifact',lambda *a,**k:{'view_name':'public.synthetic'})
    monkeypatch.setattr(engine,'_resolve_render_artifact',lambda meta,artifact:artifact)
    monkeypatch.setattr(engine,'_set_authorization_context',lambda *a:None)
    monkeypatch.setattr(module,'get_cache_settings',lambda:MagicMock(enabled=True))
    monkeypatch.setattr(module,'get_artifact_cache',lambda *a:cache)
    monkeypatch.setattr(payload_cache,'policy',lambda *a:{'definition_hash':'definition'})
    monkeypatch.setattr(payload_cache,'select_current',lambda *a:record(3))
    result=payload_cache.prewarm('client','report',record(2)['freshness'])
    assert result['status']=='superseded' and cache.redis.dbsize()==0
    assert payload_cache.prewarm('client','report',record(3)['freshness'])['status']=='prewarmed'
    assert cache.redis.ttl(next(cache.redis.scan_iter()))==-1


def test_corrupt_entry_can_be_rehydrated(monkeypatch):
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    configuration={'definition_hash':'definition'}
    cache_key=payload_cache.key(cache,'client','report','public.synthetic',configuration)
    cache.redis.set(cache_key,'{"rows":[],"definition_hash":"definition"}')
    monkeypatch.setattr(payload_cache,'policy',lambda *a:configuration)
    assert payload_cache.lookup(MagicMock(),cache,'client','report','public.synthetic')[1] is None
    assert payload_cache.publish(cache,'client','report','public.synthetic',configuration,record())
    assert cache.get(cache_key)==record()


def test_current_payload_policy_rejects_stale_or_unpublished_context(monkeypatch):
    cache=RedisCache(redis_client=fakeredis.FakeRedis(decode_responses=True))
    configuration={'definition_hash':'definition','require_current':True}
    payload_cache.publish(cache,'client','report','public.synthetic',configuration,record())
    monkeypatch.setattr(payload_cache,'policy',lambda *args:configuration)
    data=MagicMock()
    lookup=lambda stamp:payload_cache.lookup(
        data,cache,'client','report','public.synthetic',current_freshness=stamp
    )[1]
    assert lookup(record()['freshness']) == record()
    assert lookup(record(2)['freshness']) is None
    assert lookup(None) is None
    data.execute.assert_not_called()
