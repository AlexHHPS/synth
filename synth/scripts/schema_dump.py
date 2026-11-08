#!/usr/bin/env python3
"""Catalog-only dump of the Synth Supabase schema before a migration.

Writes tables, columns, constraints, indexes and policies of every schema that
holds a `meetings` table. No row data, no roles' secrets, no connection strings.
Usage: schema_dump.py <output.json>
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from synth.deploy.supabase_ops import PROJECT_REF, query  # noqa: E402

CATALOG = {
    "schemas": "SELECT DISTINCT table_schema AS schema FROM information_schema.tables WHERE table_name='meetings' ORDER BY 1",
    "columns": """SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns WHERE table_schema = ANY(%(schemas)s)
                  ORDER BY table_schema, table_name, ordinal_position""",
    "constraints": """SELECT n.nspname AS schema, c.conrelid::regclass::text AS table_name, c.conname,
                      pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c
                      JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname = ANY(%(schemas)s) ORDER BY 1,2,3""",
    "indexes": """SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes
                  WHERE schemaname = ANY(%(schemas)s) ORDER BY 1,2,3""",
    "policies": """SELECT schemaname, tablename, policyname, cmd, qual, with_check FROM pg_policies
                   WHERE schemaname = ANY(%(schemas)s) ORDER BY 1,2,3""",
    "schema_versions": "SELECT %(first)s AS schema, version, applied_at FROM {first}.schema_versions ORDER BY version",
}


def array(values):
    return "ARRAY[" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + "]::text[]"


def main():
    output = Path(sys.argv[1])
    schemas = [row["schema"] for row in query(CATALOG["schemas"])]
    if not schemas:
        raise SystemExit("no schema with a meetings table")
    dump = {"project_ref": PROJECT_REF, "schemas": schemas}
    for key in ("columns", "constraints", "indexes", "policies"):
        dump[key] = query(CATALOG[key].replace("%(schemas)s", array(schemas)))
    first = schemas[0]
    try:
        dump["schema_versions"] = query(CATALOG["schema_versions"].replace("{first}", '"' + first + '"').replace("%(first)s", "'" + first + "'"))
    except Exception as error:  # the table may not exist in older layouts
        dump["schema_versions"] = {"error": type(error).__name__}
    body = json.dumps(dump, indent=1, sort_keys=True, default=str)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body + "\n")
    print(f"schemas={schemas} tables={len({(c['table_schema'], c['table_name']) for c in dump['columns']})} "
          f"sha256={hashlib.sha256(body.encode()).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
