"""
db.py — Dual Postgres connection helpers.

Two databases run on the same host/port:
  metadata — app config, security, audit logs
  data     — client source data and report views
"""
import os
from contextlib import contextmanager
from typing import Generator

import psycopg


def _dsn(prefix: str) -> str:
    host = os.environ[f"{prefix}_HOST"]
    port = os.environ.get(f"{prefix}_PORT", "5432")
    name = os.environ[f"{prefix}_NAME"]
    user = os.environ[f"{prefix}_USER"]
    password = os.environ[f"{prefix}_PASSWORD"]
    sslmode = os.environ.get(f"{prefix}_SSLMODE", "disable")
    return (
        f"host={host} port={port} dbname={name} "
        f"user={user} password={password} sslmode={sslmode}"
    )


@contextmanager
def get_metadata_conn() -> Generator[psycopg.Connection, None, None]:
    """Connect to the metadata database (app.*, security.*, log.*)."""
    with psycopg.connect(_dsn("METADATA_DB")) as conn:
        yield conn


@contextmanager
def get_data_conn() -> Generator[psycopg.Connection, None, None]:
    """Connect to the data database (src.*, ods.*, trn.*, pds.*, rpt.*)."""
    with psycopg.connect(_dsn("DATA_DB")) as conn:
        yield conn
