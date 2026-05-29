"""
db.py — Dual Postgres connection helpers.

Two databases run on the same host/port:
  metadata — app config, security, audit logs
  data     — client source data and report views
"""
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import psycopg


def _password(prefix: str) -> str:
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    if password:
        return password

    password_file = os.environ.get(f"{prefix}_PASSWORD_FILE", "")
    if password_file:
        return Path(password_file).read_text(encoding="utf-8").strip()

    raise KeyError(f"{prefix}_PASSWORD")


def _dsn(prefix: str) -> str:
    host = os.environ[f"{prefix}_HOST"]
    port = os.environ.get(f"{prefix}_PORT", "5432")
    name = os.environ[f"{prefix}_NAME"]
    user = os.environ[f"{prefix}_USER"]
    password = _password(prefix)
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
