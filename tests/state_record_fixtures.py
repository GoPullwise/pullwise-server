"""Explicit local-fixture cutover; never an application GET-side fallback."""
import json
from pullwise_server.cloudflare_state_records import legacy_record_rows


def normalize_legacy_state(connection, *, now=0):
    rows = [{"name": row[0], "payload": row[1]} for row in connection.execute(
        "SELECT name,payload FROM app_state WHERE name NOT GLOB 'record:*'").fetchall()]
    records, sources = legacy_record_rows(rows)
    connection.execute("SAVEPOINT state_fixture_cutover")
    try:
        for name, payload in records:
            existing = connection.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()
            if existing is not None:
                if json.loads(existing[0]) != json.loads(payload):
                    raise ValueError("fixture state cutover conflicts")
            else:
                connection.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)", (name, payload, now))
        for name, previous, empty in sources:
            connection.execute("UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?",
                               (empty, now, name, previous))
    except BaseException:
        connection.execute("ROLLBACK TO state_fixture_cutover")
        connection.execute("RELEASE state_fixture_cutover")
        raise
    connection.execute("RELEASE state_fixture_cutover")
    return len(records)
