"""Opt-in persistent canonical payloads; rendering and QC remain independent."""
from datetime import datetime
import json


def policy(data, view_name):
    from .engine import _safe_view_name
    name=_safe_view_name(view_name)+'_payload_cache_policy'
    present=data.execute('SELECT to_regprocedure(%s)',(name+'()',)).fetchone()[0]
    if present is None:
        return None
    row=data.execute(f'SELECT {name}(), md5(pg_get_viewdef(%s::regclass,true))',(view_name,)).fetchone()
    value=row[0]
    if isinstance(value,str):value=json.loads(value)
    if value != {'mode':'load_bound','scope':'shared'}:
        raise ValueError('Unsupported artifact payload cache policy')
    return {'definition_hash':row[1]}


def key(cache, client_key, artifact_key, view_name, configuration):
    return cache.build_key(client_key,artifact_key,'artifact-payload',{
        'version':1,'view_name':view_name,'definition_hash':configuration['definition_hash']})


def lookup(data,cache,client_key,artifact_key,view_name):
    configuration=policy(data,view_name)
    if configuration is None:return None,None
    try:
        record=cache.get(key(cache,client_key,artifact_key,view_name,configuration)) if cache else None
    except Exception:
        record=None
    if record is not None:
        try:
            valid=(isinstance(record.get('rows'),list)
                and all(isinstance(row,dict) for row in record['rows'])
                and record.get('definition_hash')==configuration['definition_hash']
                and datetime.fromisoformat(record['freshness']).tzinfo is not None
                and type(record['generation_epoch']) in (float,int))
        except (KeyError,TypeError,ValueError):
            valid=False
        if not valid:record=None
    return configuration,record


def select_current(data,client_key,artifact_key,view_name,configuration):
    from .engine import _safe_view_name
    view_name=_safe_view_name(view_name)
    row=data.execute(
        f"SELECT public.bci_artifact_cache_freshness(%s,%s)::text, "
        f"COALESCE(jsonb_agg(to_jsonb(p)),'[]'::jsonb) FROM {view_name} p",
        (client_key,artifact_key)).fetchone()
    if not row or row[0] is None:
        raise ValueError('Load-bound payload requires a canonical generation')
    stamp=datetime.fromisoformat(str(row[0]))
    if stamp.tzinfo is None:raise ValueError('Payload generation must include timezone')
    return {'freshness':str(row[0]),'generation_epoch':stamp.timestamp(),
            'definition_hash':configuration['definition_hash'],'rows':row[1]}


def publish(cache,client_key,artifact_key,view_name,configuration,record):
    return cache.publish_load_bound_payload(
        key(cache,client_key,artifact_key,view_name,configuration),record)


def prewarm(client_key,artifact_key,expected_freshness=None):
    from . import engine
    from .cache import get_artifact_cache,get_cache_settings
    with engine.get_metadata_conn() as meta:
        artifact=engine._fetch_artifact(meta,client_key=client_key,artifact_key=artifact_key)
        if artifact is None:raise ValueError('Artifact unavailable')
        view_name=engine._safe_view_name(engine._resolve_render_artifact(meta,artifact)['view_name'])
    settings=get_cache_settings()
    cache=get_artifact_cache(settings) if settings.enabled else None
    if cache is None:raise ValueError('Payload prewarm requires available Redis')
    with engine.get_data_conn() as data:
        data.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        engine._set_authorization_context(data,client_key,None,None)
        configuration=policy(data,view_name)
        if configuration is None:raise ValueError('Artifact has not enabled shared payload caching')
        record=select_current(data,client_key,artifact_key,view_name,configuration)
    if expected_freshness is not None and (
        datetime.fromisoformat(record['freshness']) != datetime.fromisoformat(expected_freshness)):
        return {'status':'superseded','freshness':record['freshness'],'entry_count':0}
    accepted=publish(cache,client_key,artifact_key,view_name,configuration,record)
    return {'status':'prewarmed' if accepted else 'superseded',
            'freshness':record['freshness'],'entry_count':1 if accepted else 0,'ttl_seconds':0}
