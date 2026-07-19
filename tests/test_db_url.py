"""WIFI_SHEPARD_DB_URL: fail-closed URL parsing + persistence-backend selection.

One env var selects the MySQL/MariaDB backend as an alternative to the default
SQLite file; unset keeps SQLite. These tests cover the pure parts (URL parsing
and the create_database dispatch) — talking to a real MariaDB is exercised
against the live server at deploy time, not in unit tests.
"""

from __future__ import annotations

import pytest

from wifi_shepard.db import Database, Store, create_database
from wifi_shepard.db_mysql import MySQLDatabase, parse_db_url


def test_parse_full_url() -> None:
    target = parse_db_url("mysql://shep:s3cret@db.example.lan:3307/wifi_shepard")
    assert target.host == "db.example.lan"
    assert target.port == 3307
    assert target.user == "shep"
    assert target.password == "s3cret"
    assert target.database == "wifi_shepard"


def test_parse_port_defaults_to_3306() -> None:
    assert parse_db_url("mysql://u:p@host/db").port == 3306


def test_parse_accepts_mariadb_scheme() -> None:
    assert parse_db_url("mariadb://u:p@host/db").database == "db"


def test_parse_decodes_percent_encoded_password() -> None:
    target = parse_db_url("mysql://u:p%40ss%2Fword@host/db")
    assert target.password == "p@ss/word"


@pytest.mark.parametrize(
    ("url", "complaint"),
    [
        ("postgres://u:p@host/db", "scheme"),
        ("mysql://u:p@host", "database name"),
        ("mysql://u:p@host/", "database name"),
        ("mysql://u:p@host/a/b", "database name"),
        ("mysql://u@host/db", "password"),
        ("mysql://:p@host/db", "username"),
        ("mysql://u:p@/db", "host"),
    ],
)
def test_parse_fails_closed_on_malformed_url(url: str, complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        parse_db_url(url)


def test_create_database_defaults_to_sqlite(tmp_path) -> None:
    db = create_database(db_path=tmp_path / "state.db")
    assert isinstance(db, Database)


def test_create_database_empty_url_means_sqlite(tmp_path) -> None:
    # __main__ normalizes an empty env var to None; the factory tolerates both.
    db = create_database(db_path=tmp_path / "state.db", db_url=None)
    assert isinstance(db, Database)


def test_create_database_url_selects_mysql(tmp_path) -> None:
    db = create_database(
        db_path=tmp_path / "state.db",
        db_url="mysql://shep:pw@db.local:3306/wifi_shepard",
    )
    assert isinstance(db, MySQLDatabase)
    # The scan pipeline depends on the Store protocol; the MySQL backend must
    # present the exact same surface as the SQLite Database.
    assert isinstance(db, Store)


def test_create_database_bad_url_fails_closed(tmp_path) -> None:
    with pytest.raises(ValueError, match="scheme"):
        create_database(db_path=tmp_path / "state.db", db_url="postgres://u:p@host/db")
