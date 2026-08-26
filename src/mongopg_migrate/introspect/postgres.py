"""Postgres introspection: schema + FK graph, and load-order derivation.

Implements PRD §6 step 2 ("reads Postgres information_schema for target
tables, columns, types, and foreign keys") and the load-order logic needed
by migrate/load.py per PRD §7/§8 ("FK-derived load order ... parents before
children, or explicit deferred-constraint loading") and the §10 circular-FK
risk (a true cycle must be marked DEFERRABLE or it's a flagged dry-run
error, never silently reordered around).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg


class CircularDependencyError(Exception):
    """Raised when the target schema has a foreign-key cycle that is not
    fully DEFERRABLE (PRD §4 non-goal, §10 risk)."""

    def __init__(self, tables: list[str]):
        self.tables = tables
        super().__init__(
            "circular foreign-key dependency among tables "
            f"{tables} with at least one non-deferrable constraint — "
            "mark the cycle DEFERRABLE INITIALLY DEFERRED in the target DDL, "
            "or this cannot be given a safe load order"
        )


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool
    default: str | None


@dataclass
class ForeignKey:
    constraint_name: str
    column: str
    references_table: str
    references_column: str
    is_deferrable: bool


@dataclass
class TableSchema:
    name: str
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


@dataclass
class PostgresSchema:
    tables: dict[str, TableSchema] = field(default_factory=dict)

    def dependency_edges(self) -> dict[str, set[str]]:
        """table -> set of tables that must be loaded before it (its FK
        targets), excluding self-references (a table referencing itself is
        not a load-order problem — same-table rows load together)."""
        edges: dict[str, set[str]] = {t: set() for t in self.tables}
        for t, schema in self.tables.items():
            for fk in schema.foreign_keys:
                if fk.references_table != t and fk.references_table in self.tables:
                    edges[t].add(fk.references_table)
        return edges

    def load_order(self) -> list[list[str]]:
        """Returns load order as a list of "batches" — each batch is a list
        of tables that can load in any order relative to each other; batches
        must be loaded in the given sequence. A batch normally has one table;
        a batch with more than one table means those tables form a cycle
        that is fully DEFERRABLE (PRD §10) and must be loaded together
        inside a deferred-constraint transaction.

        Raises CircularDependencyError if a remaining cycle has any
        non-deferrable edge.
        """
        edges = self.dependency_edges()
        remaining = set(self.tables)
        batches: list[list[str]] = []

        while remaining:
            ready = sorted(t for t in remaining if not (edges[t] & remaining))
            if ready:
                for t in ready:
                    batches.append([t])
                remaining -= set(ready)
                continue

            # Stuck: every remaining table depends on another remaining
            # table. Check whether every FK edge *within* the remaining set
            # is deferrable — if so, that whole remaining set loads as one
            # deferred-constraint batch.
            cycle_tables = sorted(remaining)
            non_deferrable = []
            for t in cycle_tables:
                for fk in self.tables[t].foreign_keys:
                    if fk.references_table in remaining and not fk.is_deferrable:
                        non_deferrable.append(f"{t}.{fk.column} -> {fk.references_table}")
            if non_deferrable:
                raise CircularDependencyError(cycle_tables)

            batches.append(cycle_tables)
            remaining.clear()

        return batches


def introspect_postgres(dsn: str, *, schema: str = "public") -> PostgresSchema:
    result = PostgresSchema()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                """,
            (schema,),
        )
        table_names = [r[0] for r in cur.fetchall()]
        for name in table_names:
            result.tables[name] = TableSchema(name=name)

        cur.execute(
            """
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position
                """,
            (schema,),
        )
        for table_name, col_name, data_type, is_nullable, default in cur.fetchall():
            if table_name not in result.tables:
                continue
            result.tables[table_name].columns[col_name] = ColumnInfo(
                name=col_name,
                data_type=data_type,
                is_nullable=(is_nullable == "YES"),
                default=default,
            )

        cur.execute(
            """
                SELECT tc.table_name, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
                ORDER BY tc.table_name, kcu.ordinal_position
                """,
            (schema,),
        )
        for table_name, col_name in cur.fetchall():
            if table_name in result.tables:
                result.tables[table_name].primary_key.append(col_name)

        cur.execute(
            """
                SELECT
                    tc.constraint_name,
                    tc.table_name,
                    kcu.column_name,
                    ccu.table_name AS references_table,
                    ccu.column_name AS references_column,
                    tc.is_deferrable
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                 AND tc.table_schema = ccu.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
                """,
            (schema,),
        )
        for (
            constraint_name,
            table_name,
            col_name,
            ref_table,
            ref_col,
            is_deferrable,
        ) in cur.fetchall():
            if table_name not in result.tables:
                continue
            result.tables[table_name].foreign_keys.append(
                ForeignKey(
                    constraint_name=constraint_name,
                    column=col_name,
                    references_table=ref_table,
                    references_column=ref_col,
                    is_deferrable=(is_deferrable == "YES"),
                )
            )

    return result
