"""DuckDB-backed rule store + JSON export."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from planner.kb.rule_types import Rule
from planner.runs.logger import log


DDL = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id        TEXT PRIMARY KEY,
    scope          TEXT,
    statement      JSON,
    support        INTEGER,
    confidence     DOUBLE,
    exception_rate DOUBLE,
    sample_size    INTEGER,
    ci_low         DOUBLE,
    ci_high        DOUBLE,
    p_value        DOUBLE,
    type           TEXT,
    weight         DOUBLE,
    provenance     JSON,
    first_seen     DATE,
    last_seen      DATE,
    active         BOOLEAN
);
"""


def open_store(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(DDL)
    return con


def save_rules(con: duckdb.DuckDBPyConnection, rules: list[Rule]) -> int:
    if not rules:
        return 0
    con.execute("BEGIN")
    for r in rules:
        con.execute(
            """
            INSERT OR REPLACE INTO rules VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [
                r.rule_id, r.scope, json.dumps(r.statement),
                r.support, r.confidence, r.exception_rate, r.sample_size,
                r.ci_low, r.ci_high, r.p_value,
                r.type.value, r.weight, json.dumps(r.provenance),
                r.first_seen, r.last_seen, r.active,
            ],
        )
    con.execute("COMMIT")
    log.info("kb.save_rules", n=len(rules))
    return len(rules)


def export_json(con: duckdb.DuckDBPyConnection, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT * FROM rules ORDER BY type, scope, rule_id").fetchall()
    cols = [d[0] for d in con.description]
    data = [dict(zip(cols, r)) for r in rows]
    # DuckDB returns JSON columns as strings — decode for pretty export
    for d in data:
        for k in ("statement", "provenance"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        for k in ("first_seen", "last_seen"):
            if d.get(k) is not None:
                d[k] = str(d[k])
    out.write_text(json.dumps(data, indent=2, default=str))
    log.info("kb.export_json", path=str(out), n=len(data))
