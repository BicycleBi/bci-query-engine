"""Exercise the authorization SQL against fixture-only identity assignments."""
from contextlib import contextmanager
import re
import sqlite3

import pytest
from fastapi import HTTPException
from app import usage_access


@pytest.mark.parametrize('subject,email,active,expiry,resource,group,allowed', [
 ('canonical-user',None,True,None,'artifact:srp:usage-monitoring-dashboard',False,True),
 ('provider-oid','ADMIN@example.test',True,None,'artifact:srp:usage-monitoring-dashboard',False,True),
 ('provider-oid','admin@example.test',True,None,'artifact:srp:usage-monitoring-dashboard',True,True),
 ('provider-oid','admin@example.test',False,None,'artifact:srp:usage-monitoring-dashboard',False,False),
 ('provider-oid','admin@example.test',True,'2000-01-01','artifact:srp:usage-monitoring-dashboard',False,False),
 ('provider-oid','admin@example.test',True,None,'artifact:srp:*',False,False),
 ('provider-oid','other@example.test',True,None,'artifact:srp:usage-monitoring-dashboard',False,False),
 ('provider-oid',None,True,None,'artifact:srp:usage-monitoring-dashboard',False,False),
])
def test_trusted_identity_mapping_preserves_exact_live_grant(monkeypatch, subject,email,active,expiry,resource,group,allowed):
 db=sqlite3.connect(':memory:')
 db.executescript('''
 CREATE TABLE security_users(user_id TEXT,email TEXT,active BOOLEAN);
 CREATE TABLE security_user_roles(user_id TEXT,role_key TEXT,client_key TEXT,expires_at TEXT);
 CREATE TABLE security_group_members(user_id TEXT,group_key TEXT,client_key TEXT,expires_at TEXT);
 CREATE TABLE security_group_roles(group_key TEXT,role_key TEXT,client_key TEXT,expires_at TEXT);
 CREATE TABLE security_role_permissions(role_key TEXT,resource_key TEXT,permission_key TEXT);
 ''')
 db.execute('INSERT INTO security_users VALUES(?,?,?)',('canonical-user','admin@example.test',active))
 if group:
  db.execute('INSERT INTO security_group_members VALUES(?,?,?,?)',('canonical-user','synthetic-group','srp',expiry))
  db.execute('INSERT INTO security_group_roles VALUES(?,?,?,NULL)',('synthetic-group','srpdev_bicycle_dev','srp'))
 else:db.execute('INSERT INTO security_user_roles VALUES(?,?,?,?)',('canonical-user','srpdev_bicycle_dev','srp',expiry))
 db.execute('INSERT INTO security_role_permissions VALUES(?,?,?)',('srpdev_bicycle_dev',resource,'usage:read'))
 class FixtureSQL:
  def execute(self,sql,params=None):
   if sql=='SET TRANSACTION READ ONLY':return None
   # Adapt only psycopg bind syntax and PostgreSQL literal casts; execute the real predicate.
   sql=re.sub(r'%\((\w+)\)s',r':\1',sql.replace('::text',''))
   params=dict(params);params['now']=params['now'].isoformat()
   return db.execute(sql,params)
 @contextmanager
 def fixture_conn():yield FixtureSQL()
 monkeypatch.setattr(usage_access,'get_metadata_conn',fixture_conn)
 identity={'client_key':'srp','sub':subject,'email':email,'roles':['untrusted-admin-label']}
 try:
  if allowed:usage_access.require_usage_reporting_access(identity,'srp')
  else:
   with pytest.raises(HTTPException) as exc:usage_access.require_usage_reporting_access(identity,'srp')
   assert exc.value.status_code==403
 finally:db.close()


def test_dedicated_analytics_role_can_read_access_but_not_usage(monkeypatch):
 db=sqlite3.connect(':memory:')
 db.executescript('''
 CREATE TABLE security_users(user_id TEXT,email TEXT,active BOOLEAN);
 CREATE TABLE security_user_roles(user_id TEXT,role_key TEXT,client_key TEXT,expires_at TEXT);
 CREATE TABLE security_group_members(user_id TEXT,group_key TEXT,client_key TEXT,expires_at TEXT);
 CREATE TABLE security_group_roles(group_key TEXT,role_key TEXT,client_key TEXT,expires_at TEXT);
 CREATE TABLE security_role_permissions(role_key TEXT,resource_key TEXT,permission_key TEXT);
 INSERT INTO security_users VALUES('corporate-user','corporate@example.test',1);
 INSERT INTO security_user_roles VALUES('corporate-user','srpqa_bci_analytics','srp',NULL);
 INSERT INTO security_role_permissions VALUES('srpqa_bci_analytics','artifact:srp:usage-monitoring-dashboard','artifact:read');
 ''')
 class FixtureSQL:
  def execute(self,sql,params=None):
   if sql=='SET TRANSACTION READ ONLY':return None
   sql=re.sub(r'%\((\w+)\)s',r':\1',sql.replace('::text',''))
   params=dict(params);params['now']=params['now'].isoformat()
   return db.execute(sql,params)
 @contextmanager
 def fixture_conn():yield FixtureSQL()
 monkeypatch.setenv('SRP_BICYCLE_ADMIN_ROLE','srpqa_admin')
 monkeypatch.setenv('SRP_CORPORATE_ANALYTICS_ROLE','srpqa_bci_analytics')
 monkeypatch.setattr(usage_access,'get_metadata_conn',fixture_conn)
 identity={'client_key':'srp','sub':'corporate-user','email':'corporate@example.test'}
 assert usage_access.require_analytics_reporting_access(identity,'srp','access') is False
 with pytest.raises(HTTPException) as exc:
   usage_access.require_analytics_reporting_access(identity,'srp','usage')
 assert exc.value.status_code==403
 db.close()
