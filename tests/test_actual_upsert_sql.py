"""Regression: with-id actual upserts against Postgres.

calendar_actuals.actual_id is GENERATED ALWAYS AS IDENTITY
(schema 001), so the with-id upsert must OVERRIDING SYSTEM VALUE or
Postgres rejects it ("cannot insert a non-DEFAULT value into column
actual_id"). The service-level tests run on the in-memory store and
cannot see this; pin the SQL shape here instead. The no-id insert
must keep omitting the column entirely.
"""

from kalends.store import PostgresStore


def test_with_id_upsert_overrides_identity():
    sql = PostgresStore._ACTUAL_UPSERT_WITH_ID
    assert "OVERRIDING SYSTEM VALUE" in sql
    assert "ON CONFLICT (actual_id) DO UPDATE" in sql


def test_plain_insert_omits_identity_column():
    sql = PostgresStore._ACTUAL_INSERT
    assert "actual_id" not in sql.split("VALUES")[0]
    assert "OVERRIDING" not in sql
