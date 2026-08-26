"""CLI entry point. Commands map onto PRD §6's user flow:

    introspect        -> §6 step 2
    propose            -> §6 step 3
    validate-mapping    -> §6 step 4 (structural + unmapped-field policy)
    dry-run             -> §6 step 5 (two-layer: fast in-memory pass + realistic temp-schema pass — see migrate/dryrun.py)
    migrate              -> §6 step 6 (entity-ordered COPY + id_map + checkpoint/resume — see migrate/load.py)
    validate             -> §6 step 7 (count diff + hashed-field sample diff — see report/validate.py)

Connection strings follow the PRD §8 Docker example: MONGO_URI / POSTGRES_URI
env vars, or --mongo-uri / --postgres-uri flags.
"""

from __future__ import annotations

import json
import sys

import click
import psycopg

from mongopg_migrate.introspect.mongo import introspect_database
from mongopg_migrate.introspect.postgres import CircularDependencyError, introspect_postgres
from mongopg_migrate.mapping.llm_client import DEFAULT_MODEL as DEFAULT_LLM_MODEL
from mongopg_migrate.mapping.llm_client import AnthropicLLMClient
from mongopg_migrate.mapping.llm_propose import enrich_mapping_with_llm
from mongopg_migrate.mapping.propose import propose_mapping
from mongopg_migrate.mapping.schema import (
    CircularEntityDependencyError,
    dump_mapping_file,
    load_mapping_file,
    validate_against_mongo_schema,
    validate_structure,
)
from mongopg_migrate.migrate import dryrun
from mongopg_migrate.migrate.load import LoadError
from mongopg_migrate.migrate.load import load as run_load
from mongopg_migrate.report.validate import ValidationError
from mongopg_migrate.report.validate import validate as run_validate

MONGO_URI_OPTION = click.option(
    "--mongo-uri", envvar="MONGO_URI", required=True, help="Source MongoDB connection string."
)
POSTGRES_URI_OPTION = click.option(
    "--postgres-uri", envvar="POSTGRES_URI", required=True, help="Target PostgreSQL connection string."
)


@click.group()
@click.version_option()
def main() -> None:
    """mongopg-migrate: map MongoDB collections onto an existing,
    independently designed PostgreSQL schema."""


@main.command("introspect")
@MONGO_URI_OPTION
@POSTGRES_URI_OPTION
@click.option("--sample-size", type=int, default=None, help="Override the sampling heuristic (PRD §10).")
@click.option("--pg-schema", default="public", help="Postgres schema to introspect.")
def introspect_cmd(mongo_uri: str, postgres_uri: str, sample_size: int | None, pg_schema: str) -> None:
    """Sample Mongo collections and read the Postgres target schema; print
    a JSON summary of both (PRD §6 step 2)."""
    click.echo("Introspecting MongoDB...", err=True)
    mongo_schemas = introspect_database(mongo_uri, sample_size=sample_size)

    click.echo("Introspecting PostgreSQL...", err=True)
    pg = introspect_postgres(postgres_uri, schema=pg_schema)

    try:
        load_order = pg.load_order()
    except CircularDependencyError as e:
        click.echo(f"WARNING: {e}", err=True)
        load_order = None

    out = {
        "mongo": {
            name: {
                "document_count": c.document_count,
                "sampled_count": c.sampled_count,
                "polymorphism_candidate": c.polymorphism_candidate,
                "discriminator_field": c.discriminator_field,
                "fields": {p: s.as_dict() for p, s in sorted(c.fields.items())},
            }
            for name, c in mongo_schemas.items()
        },
        "postgres": {
            "tables": {
                name: {
                    "columns": {
                        cn: {"data_type": ci.data_type, "is_nullable": ci.is_nullable}
                        for cn, ci in t.columns.items()
                    },
                    "primary_key": t.primary_key,
                    "foreign_keys": [
                        {
                            "column": fk.column,
                            "references": f"{fk.references_table}.{fk.references_column}",
                            "is_deferrable": fk.is_deferrable,
                        }
                        for fk in t.foreign_keys
                    ],
                }
                for name, t in pg.tables.items()
            },
            "load_order": load_order,
        },
    }
    click.echo(json.dumps(out, indent=2, default=str))


@main.command("propose")
@MONGO_URI_OPTION
@POSTGRES_URI_OPTION
@click.option("--sample-size", type=int, default=None)
@click.option("--pg-schema", default="public")
@click.option("-o", "--output", default="mapping.yaml", help="Where to write the proposed mapping file.")
@click.option(
    "--llm/--no-llm",
    default=False,
    help="Ask an LLM to suggest mappings for fields name-similarity couldn't resolve (PRD §7/§8 P1). "
    "Off by default. Sends only schema metadata (field names/types/shapes), never row data. "
    "Requires `pip install mongopg-migrate[llm]` and Anthropic credentials.",
)
@click.option("--llm-model", default=DEFAULT_LLM_MODEL)
def propose_cmd(
    mongo_uri: str,
    postgres_uri: str,
    sample_size: int | None,
    pg_schema: str,
    output: str,
    llm: bool,
    llm_model: str,
) -> None:
    """Generate a candidate mapping.yaml (PRD §6 step 3). Always review and
    edit before running `migrate` — nothing here is auto-confirmed."""
    click.echo("Introspecting MongoDB...", err=True)
    mongo_schemas = introspect_database(mongo_uri, sample_size=sample_size)
    click.echo("Introspecting PostgreSQL...", err=True)
    pg = introspect_postgres(postgres_uri, schema=pg_schema)

    mapping, issues = propose_mapping(mongo_schemas, pg)

    if llm:
        click.echo("Asking LLM for suggestions on fields name-similarity couldn't resolve...", err=True)
        llm_client = AnthropicLLMClient(model=llm_model)
        issues += enrich_mapping_with_llm(llm_client, mapping, mongo_schemas, pg)

    dump_mapping_file(mapping, output)
    click.echo(f"Wrote candidate mapping to {output}", err=True)

    if issues:
        click.echo(
            f"\n{len(issues)} item(s) flagged for review — nothing below was silently guessed:\n",
            err=True,
        )
        for issue in issues:
            loc = f"{issue.entity}" + (f".{issue.field}" if issue.field else "")
            conf = f" (confidence={issue.confidence:.2f})" if issue.confidence is not None else ""
            click.echo(f"  [{loc}] {issue.message}{conf}", err=True)
    click.echo(
        "\nReview and edit the mapping file before running `validate-mapping` and `migrate` "
        "(PRD §6 step 4 — nothing is written to Postgres until you confirm it).",
        err=True,
    )


@main.command("validate-mapping")
@click.argument("mapping_path")
@click.option("--mongo-uri", envvar="MONGO_URI", default=None, help="If given, also checks the P0 unmapped-field policy against live Mongo schema.")
@click.option("--sample-size", type=int, default=None)
def validate_mapping_cmd(mapping_path: str, mongo_uri: str | None, sample_size: int | None) -> None:
    """Validate a mapping file: structure (PRD §12) always; the P0
    unmapped-field policy (PRD §7) too, if --mongo-uri is given."""
    mapping = load_mapping_file(mapping_path)
    issues = validate_structure(mapping)

    if mongo_uri:
        click.echo("Introspecting MongoDB to check unmapped-field policy...", err=True)
        sources = {e.source for e in mapping.entities.values()}
        mongo_schemas = introspect_database(mongo_uri, collections=list(sources), sample_size=sample_size)
        fields_by_entity = {
            name: mongo_schemas[e.source].top_level_field_names()
            for name, e in mapping.entities.items()
            if e.source in mongo_schemas
        }
        issues += validate_against_mongo_schema(mapping, fields_by_entity)

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for i in warnings:
        loc = f"{i.entity}" + (f".{i.field}" if i.field else "")
        click.echo(f"WARNING [{loc}] {i.message}", err=True)
    for i in errors:
        loc = f"{i.entity}" + (f".{i.field}" if i.field else "")
        click.echo(f"ERROR [{loc}] {i.message}", err=True)

    if errors:
        click.echo(f"\n{len(errors)} error(s) — mapping file is not valid.", err=True)
        sys.exit(1)
    click.echo(f"OK — mapping file is structurally valid ({len(warnings)} warning(s)).", err=True)


@main.command("dry-run")
@click.argument("mapping_path")
@MONGO_URI_OPTION
@POSTGRES_URI_OPTION
@click.option("--pg-schema", default="public")
@click.option("--batch-size", type=int, default=500)
@click.option("--sample-size", type=int, default=None, help="Limit Layer A to a sample instead of the full dataset.")
@click.option(
    "--realistic/--no-realistic",
    default=None,
    help="Force Layer B on/off. Default: run it only if Layer A found nothing (see migrate/dryrun.py:run).",
)
def dry_run_cmd(
    mapping_path: str,
    mongo_uri: str,
    postgres_uri: str,
    pg_schema: str,
    batch_size: int,
    sample_size: int | None,
    realistic: bool | None,
) -> None:
    """PRD §6 step 5: in-memory type/null/lookup validation (Layer A),
    optionally followed by a real COPY+FK load into a disposable schema
    clone (Layer B). Nothing here ever writes to the real target tables."""
    mapping = load_mapping_file(mapping_path)

    structural_issues = validate_structure(mapping)
    errors = [i for i in structural_issues if i.severity == "error"]
    if errors:
        for i in errors:
            click.echo(f"ERROR [{i.entity}] {i.message}", err=True)
        click.echo("\nFix the mapping file (see `validate-mapping`) before running dry-run.", err=True)
        sys.exit(1)

    click.echo("Introspecting PostgreSQL...", err=True)
    pg = introspect_postgres(postgres_uri, schema=pg_schema)

    try:
        if realistic is None:
            report = dryrun.run(
                mapping, mongo_uri, postgres_uri, pg, batch_size=batch_size, sample_size=sample_size
            )
        elif realistic:
            click.echo("Running Layer A (fast pass)...", err=True)
            report = dryrun.run_fast_pass(mapping, mongo_uri, pg, batch_size=batch_size, sample_size=sample_size)
            click.echo("Running Layer B (realistic pass, disposable schema clone)...", err=True)
            realistic_report = dryrun.run_realistic_pass(mapping, mongo_uri, postgres_uri, pg, batch_size=batch_size)
            report.violations.extend(realistic_report.violations)
        else:
            report = dryrun.run_fast_pass(mapping, mongo_uri, pg, batch_size=batch_size, sample_size=sample_size)
    except CircularEntityDependencyError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)

    for v in report.violations:
        loc = f"{v.entity}" + (f".{v.field}" if v.field else "")
        click.echo(f"[{v.layer}] [{loc}] {v.message}", err=True)
    if report.truncated:
        click.echo(
            f"\n... stopped after {len(report.violations)} violation(s) — fix these and re-run "
            "to see more.",
            err=True,
        )

    if not report.ok:
        click.echo(f"\n{len(report.violations)} violation(s) found — do not run migrate yet.", err=True)
        sys.exit(1)
    click.echo("\nOK — dry-run found no violations.", err=True)


@main.command("migrate")
@click.argument("mapping_path")
@MONGO_URI_OPTION
@POSTGRES_URI_OPTION
@click.option(
    "--mode",
    type=click.Choice(["truncate", "append", "upsert"]),
    required=True,
    help="No default — an explicit choice is required (PRD §6 step 6: truncate must never be silently assumed). "
    "upsert requires a unique constraint on each junction table's two FK columns; explode child tables "
    "(SERIAL keys, no natural conflict target) always insert regardless of mode.",
)
@click.option("--pg-schema", default="public")
@click.option("--batch-size", type=int, default=500)
def migrate_cmd(
    mapping_path: str, mongo_uri: str, postgres_uri: str, mode: str, pg_schema: str, batch_size: int
) -> None:
    """PRD §6 step 6: FK/lookup-ordered COPY + `_mongopg.id_map` + per-batch
    checkpoint/resume. Re-running after a kill resumes automatically."""
    mapping = load_mapping_file(mapping_path)

    structural_issues = validate_structure(mapping)
    errors = [i for i in structural_issues if i.severity == "error"]
    if errors:
        for i in errors:
            click.echo(f"ERROR [{i.entity}] {i.message}", err=True)
        click.echo("\nFix the mapping file (see `validate-mapping`) before running migrate.", err=True)
        sys.exit(1)

    if mode == "upsert":
        exploding_entities = [name for name, e in mapping.entities.items() if e.explode]
        if exploding_entities:
            click.echo(
                f"NOTE: --mode upsert re-inserts (does not deduplicate) rows in explode child "
                f"tables for: {', '.join(exploding_entities)} — their child tables have a SERIAL "
                "key with no natural conflict target. Re-running upsert over already-loaded "
                "documents will duplicate those child rows. Use --mode truncate for a clean "
                "re-migration instead if that matters here.",
                err=True,
            )

    click.echo("Introspecting PostgreSQL (for FK graph, column types, and truncate order)...", err=True)
    pg = introspect_postgres(postgres_uri, schema=pg_schema)

    try:
        summary = run_load(mapping, mongo_uri, postgres_uri, pg, mode=mode, batch_size=batch_size)
    except CircularEntityDependencyError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    except CircularDependencyError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    except LoadError as e:
        click.echo(f"ERROR: {e}", err=True)
        click.echo(
            "\nThe transaction for the in-progress batch was rolled back; already-committed "
            "batches are unaffected. Fix the issue and re-run — it will resume from the last "
            "checkpoint (see _mongopg.load_checkpoint).",
            err=True,
        )
        sys.exit(1)
    except psycopg.errors.UniqueViolation as e:
        click.echo(f"ERROR: {e}", err=True)
        click.echo(
            "\nThat's a primary-key conflict, not a data-quality problem: some rows for this "
            "entity already exist in the target. `--mode append` requires the target to hold "
            "only new rows — re-run with `--mode upsert` if you want to update existing rows "
            "for a re-migrated/changed source document, or `--mode truncate` to start over. "
            "The in-progress batch's transaction was rolled back; already-committed batches "
            "are unaffected.",
            err=True,
        )
        sys.exit(1)
    except psycopg.Error as e:
        click.echo(f"ERROR: Postgres error: {e}", err=True)
        click.echo(
            "\nThe transaction for the in-progress batch was rolled back; already-committed "
            "batches are unaffected.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"\nmode={summary.mode}", err=True)
    for r in summary.results:
        if r.already_done:
            click.echo(f"  {r.entity}: already fully loaded (skipped)", err=True)
        else:
            resumed = f", resumed after {r.resumed_from}" if r.resumed_from else ""
            click.echo(f"  {r.entity}: {r.rows_loaded} row(s) loaded{resumed}", err=True)
    click.echo(
        "\nRun `validate` (PRD §6 step 7) to check counts and value-level diffs before "
        "trusting this migration.",
        err=True,
    )


@main.command("validate")
@click.argument("mapping_path")
@MONGO_URI_OPTION
@POSTGRES_URI_OPTION
@click.option("--pg-schema", default="public")
@click.option(
    "--sample-size",
    type=int,
    default=200,
    help="Random rows per entity to re-derive and value-diff against the loaded row (PRD §9).",
)
def validate_cmd(
    mapping_path: str, mongo_uri: str, postgres_uri: str, pg_schema: str, sample_size: int
) -> None:
    """PRD §6 step 7: post-migration count diff (incl. explode/junction
    child tables) plus a hashed-field sample diff — row counts matching is
    necessary but not sufficient; this also re-derives sampled rows' values
    from Mongo and compares them to what actually landed in Postgres."""
    mapping = load_mapping_file(mapping_path)

    click.echo("Introspecting PostgreSQL...", err=True)
    pg = introspect_postgres(postgres_uri, schema=pg_schema)

    try:
        report = run_validate(mapping, mongo_uri, postgres_uri, pg, sample_size=sample_size)
    except ValidationError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)

    click.echo("Count diff:", err=True)
    for c in report.count_diffs:
        marker = "OK" if c.matches else "MISMATCH"
        click.echo(f"  [{marker}] {c.entity} ({c.table}): mongo={c.mongo_count} postgres={c.postgres_count}", err=True)

    click.echo(f"\nSample diff ({report.sampled_rows} row(s) sampled):", err=True)
    if report.sample_diffs:
        for d in report.sample_diffs:
            click.echo(f"  [MISMATCH] {d.entity} source_id={d.source_id}: {', '.join(d.mismatched_fields)}", err=True)
    else:
        click.echo("  no mismatches", err=True)

    if not report.ok:
        click.echo("\nValidation FAILED — do not trust this migration as-is.", err=True)
        sys.exit(1)
    click.echo("\nOK — counts match and the sampled rows' values are correct.", err=True)


if __name__ == "__main__":
    main()
