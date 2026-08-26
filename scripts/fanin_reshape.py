#!/usr/bin/env python3
"""Fan-in reshape helper — Stage 0 for the "N Mongo documents -> 1 target
row" case the core tool deliberately does not support in its mapping DSL
(PRD-mongo-postgres-migration-tool.md §4 non-goal: "No fan-in aggregation
in the mapping DSL"). Every construct `mongopg-migrate` understands
(`fields`, `explode`, `junction`, `unpivot`) describes one source document
producing rows — none of them can express, say, "the latest
KycVerificationStep document per booking" collapsing N step-history
documents into one row. That's grouping/aggregation, a genuinely different
shape of work from field mapping, so it's kept out of the core tool and
out of `src/mongopg_migrate` entirely.

What this script actually is: a small, safety-conscious wrapper around the
standard Mongo aggregation pattern for that reshape —

    $match (optional) -> $sort -> $group (pick one doc per key) ->
    $replaceRoot (restore the original document shape) -> $out | $merge

— run once, before `mongopg-migrate introspect`/`propose`/`migrate`, against
the *source* Mongo database, producing a new derived collection that IS
already one-to-one-shaped and can be mapped with the core tool exactly like
any other collection. It does not touch Postgres and does not know
anything about mapping.yaml.

Two ways to pick the "winning" document per group:
  --group-by + --pick-latest-by   the common case: pick one doc per group
                                   by max (or min, --pick-order asc) of a
                                   sort field (e.g. `updatedAt`).
  --pipeline-file                 escape hatch for anything the simple
                                   case can't express (e.g. "prefer
                                   status=APPROVED over PENDING, then
                                   latest") — supply your own pipeline
                                   *up to and including* the $group/
                                   $replaceRoot stages; this script adds
                                   the $match (if --filter given) in
                                   front and the $out/$merge stage at the
                                   end, and gives you the same dry-run/
                                   confirmation/count-report plumbing
                                   either way.

IMPORTANT caveat this script cannot paper over (stated in the PRD, restated
here): once you migrate the *derived* collection instead of the original,
`mongopg-migrate validate`'s count/sample diff is checking the derived
collection against Postgres, not the original source collection against
Postgres. That's a moved verification boundary, not a removed risk — the
N->1 reduction itself is never re-verified by anything downstream of this
script. Look at the dry-run preview before you commit to writing `--mode
out|merge`.
"""

from __future__ import annotations

import json
import sys

import click
import pymongo.errors
from pymongo import MongoClient


class ReshapeError(Exception):
    pass


def build_pickone_pipeline(
    group_by: list[str],
    pick_field: str,
    *,
    pick_order: str = "desc",
    match_filter: dict | None = None,
) -> list[dict]:
    """The standard "one row per group, chosen by max/min of a field"
    pipeline. `$group` on multiple fields groups on the tuple of them (via
    a sub-document `_id`); `$replaceRoot` afterward means the *output*
    documents come back in their original, un-nested shape — group-by
    field values included, exactly as they were on the source document —
    which is what makes the result mappable by the core tool like any
    other collection, not something requiring an `unpivot`-style `_id`
    disposition of its own.
    """
    if pick_order not in ("desc", "asc"):
        raise ReshapeError(f"pick_order must be 'desc' or 'asc', got {pick_order!r}")
    sort_dir = -1 if pick_order == "desc" else 1
    group_id = {f: f"${f}" for f in group_by} if len(group_by) > 1 else f"${group_by[0]}"
    pipeline: list[dict] = []
    if match_filter:
        pipeline.append({"$match": match_filter})
    pipeline.append({"$sort": {pick_field: sort_dir}})
    pipeline.append({"$group": {"_id": group_id, "doc": {"$first": "$$ROOT"}}})
    pipeline.append({"$replaceRoot": {"newRoot": "$doc"}})
    return pipeline


def build_output_stage(mode: str, dest: str, merge_on: list[str] | None) -> dict:
    if mode == "out":
        return {"$out": dest}
    if mode == "merge":
        if not merge_on:
            raise ReshapeError("mode=merge requires merge_on (the natural key to upsert derived docs by)")
        on = merge_on if len(merge_on) > 1 else merge_on[0]
        return {
            "$merge": {
                "into": dest,
                "on": on,
                "whenMatched": "replace",
                "whenNotMatched": "insert",
            }
        }
    raise ReshapeError(f"mode must be 'out' or 'merge', got {mode!r}")


def load_custom_pipeline(path: str) -> list[dict]:
    with open(path) as f:
        pipeline = json.load(f)
    if not isinstance(pipeline, list) or not all(isinstance(s, dict) for s in pipeline):
        raise ReshapeError(f"{path}: expected a JSON array of aggregation pipeline stage objects")
    for stage in pipeline:
        if "$out" in stage or "$merge" in stage:
            raise ReshapeError(
                f"{path}: pipeline must not include its own $out/$merge stage — this script appends "
                "the output stage itself, driven by --mode/--dest/--merge-on, so the same --dry-run "
                "preview and confirmation gate apply regardless of which pipeline mode you used"
            )
    return pipeline


def run_reshape(
    mongo_uri: str,
    database: str,
    source: str,
    dest: str,
    pipeline: list[dict],
    *,
    mode: str,
    merge_on: list[str] | None,
    dry_run: bool,
    preview_limit: int = 5,
) -> dict:
    if source == dest:
        raise ReshapeError(f"source and dest are both {source!r} — refusing to reshape a collection into itself")

    client = MongoClient(mongo_uri)
    try:
        db = client[database]
        source_count = db[source].count_documents({})

        if dry_run:
            preview = list(db[source].aggregate([*pipeline, {"$limit": preview_limit}]))
            group_count_result = list(db[source].aggregate([*pipeline, {"$count": "n"}]))
            group_count = group_count_result[0]["n"] if group_count_result else 0
            return {
                "dry_run": True,
                "source_count": source_count,
                "would_write_count": group_count,
                "preview": preview,
            }

        output_stage = build_output_stage(mode, dest, merge_on)
        list(db[source].aggregate([*pipeline, output_stage]))
        dest_count = db[dest].count_documents({})
        return {
            "dry_run": False,
            "source_count": source_count,
            "dest_count": dest_count,
        }
    finally:
        client.close()


def _split_fields(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [f.strip() for f in value.split(",") if f.strip()]


@click.command()
@click.option("--mongo-uri", envvar="MONGO_URI", required=True, help="Source MongoDB connection string.")
@click.option("--database", required=True, help="Database name (mongopg-migrate's other commands take this from the URI path; pymongo aggregation needs it explicit here).")
@click.option("--source", required=True, help="Source collection to reshape (read-only — never written to).")
@click.option("--dest", required=True, help="Destination collection for the derived, 1:1-shaped documents.")
@click.option("--group-by", default=None, help="Comma-separated field name(s) to group by, e.g. bookingId. Required unless --pipeline-file is given.")
@click.option("--pick-latest-by", default=None, help="Field whose max (or min, see --pick-order) value decides the winning document per group, e.g. updatedAt. Required unless --pipeline-file is given.")
@click.option("--pick-order", type=click.Choice(["desc", "asc"]), default="desc", show_default=True, help="desc = max of --pick-latest-by wins ('latest'); asc = min wins ('earliest').")
@click.option("--filter", "filter_json", default=None, help="Optional JSON object: an extra $match applied before grouping, e.g. '{\"env\": \"prod\"}'.")
@click.option("--pipeline-file", default=None, type=click.Path(exists=True, dir_okay=False), help="Escape hatch: a JSON file containing your own pipeline stages (through $group/$replaceRoot) for grouping logic --group-by/--pick-latest-by can't express. Mutually exclusive with --group-by/--pick-latest-by.")
@click.option("--mode", type=click.Choice(["out", "merge"]), required=True, help="No default — explicit choice required. 'out' REPLACES dest's entire contents every run. 'merge' upserts by --merge-on (or --group-by), leaving unrelated existing dest documents alone — requires a pre-existing UNIQUE index on dest covering the --merge-on field(s), or Mongo rejects the write.")
@click.option("--merge-on", default=None, help="Comma-separated natural key field(s) to upsert on for --mode merge. Defaults to --group-by when using --group-by/--pick-latest-by; required (no default) when using --pipeline-file, since this script can't infer your pipeline's grouping key.")
@click.option("--dry-run", is_flag=True, default=False, help="Preview only: prints the pipeline, the would-be output row count, and a sample of resulting documents. Writes nothing.")
@click.option("--preview-limit", type=int, default=5, show_default=True, help="Documents to show in --dry-run output.")
@click.option("-y", "--yes", is_flag=True, default=False, help="Skip the confirmation prompt before writing (for scripted/CI use).")
def main(
    mongo_uri: str,
    database: str,
    source: str,
    dest: str,
    group_by: str | None,
    pick_latest_by: str | None,
    pick_order: str,
    filter_json: str | None,
    pipeline_file: str | None,
    mode: str,
    merge_on: str | None,
    dry_run: bool,
    preview_limit: int,
    yes: bool,
) -> None:
    """Reshape N Mongo documents per group into 1 derived document each,
    written to --dest — the Stage-0 step for a fan-in case
    (mongopg-migrate's mapping DSL deliberately can't express this; see
    PRD §4). Point mongopg-migrate's mapping file at the resulting --dest
    collection afterward, not the original --source collection."""
    group_by_fields = _split_fields(group_by)
    merge_on_fields = _split_fields(merge_on)
    match_filter = json.loads(filter_json) if filter_json else None

    try:
        if pipeline_file:
            if group_by_fields or pick_latest_by:
                raise ReshapeError("--pipeline-file is mutually exclusive with --group-by/--pick-latest-by")
            pipeline = load_custom_pipeline(pipeline_file)
            if match_filter:
                pipeline = [{"$match": match_filter}, *pipeline]
            if mode == "merge" and not merge_on_fields:
                raise ReshapeError("--mode merge with --pipeline-file requires --merge-on (no default here — "
                                    "this script doesn't know your custom pipeline's grouping key)")
        else:
            if not group_by_fields or not pick_latest_by:
                raise ReshapeError("--group-by and --pick-latest-by are both required unless --pipeline-file is given")
            pipeline = build_pickone_pipeline(
                group_by_fields, pick_latest_by, pick_order=pick_order, match_filter=match_filter
            )
            if mode == "merge" and not merge_on_fields:
                merge_on_fields = group_by_fields  # the natural default: upsert on the same key you grouped by
    except ReshapeError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)

    click.echo("Pipeline:", err=True)
    click.echo(json.dumps(pipeline, indent=2, default=str), err=True)
    if mode == "merge":
        click.echo(f"Output: $merge into {dest!r} on {merge_on_fields} (whenMatched=replace, whenNotMatched=insert)", err=True)
    else:
        click.echo(f"Output: $out to {dest!r} (REPLACES the entire collection every run)", err=True)

    try:
        if dry_run:
            result = run_reshape(
                mongo_uri, database, source, dest, pipeline,
                mode=mode, merge_on=merge_on_fields, dry_run=True, preview_limit=preview_limit,
            )
            click.echo(
                f"\n[DRY RUN] {result['source_count']} source document(s) -> "
                f"would write {result['would_write_count']} document(s) to {dest!r}",
                err=True,
            )
            if result["preview"]:
                click.echo(f"\nPreview (first {len(result['preview'])}):", err=True)
                click.echo(json.dumps(result["preview"], indent=2, default=str), err=True)
            click.echo("\nNothing written — re-run without --dry-run to write.", err=True)
            return

        if not yes:
            action = f"REPLACE all of {dest!r}" if mode == "out" else f"upsert into {dest!r}"
            if not click.confirm(f"\nThis will {action} in database {database!r}. Continue?", default=False):
                click.echo("Aborted — nothing written.", err=True)
                sys.exit(1)

        result = run_reshape(
            mongo_uri, database, source, dest, pipeline,
            mode=mode, merge_on=merge_on_fields, dry_run=False,
        )
    except ReshapeError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    except pymongo.errors.OperationFailure as e:
        click.echo(f"ERROR: {e}", err=True)
        if mode == "merge" and e.code == 51183:  # "Cannot find index to verify that join fields will be unique"
            click.echo(
                f"\n$merge requires a UNIQUE index on {dest!r} covering the --merge-on field(s) "
                f"({merge_on_fields}) before it will write anything — e.g.: "
                f"db.{dest}.createIndex({{{', '.join(f'{f!r}: 1' for f in (merge_on_fields or []))}}}, "
                "{unique: true}). Nothing was written.",
                err=True,
            )
        sys.exit(1)

    click.echo(
        f"\nDone: {result['source_count']} source document(s) in {source!r} -> "
        f"{result['dest_count']} document(s) now in {dest!r}",
        err=True,
    )
    click.echo(
        f"\nPoint mongopg-migrate's mapping file at `source: {dest}` from here on — not the original "
        f"collection ({source!r}). Note: `validate`'s count/sample diff will then check this derived "
        "collection against Postgres, not the original source collection; the N->1 reduction itself "
        "is not re-verified by anything downstream of this script.",
        err=True,
    )


if __name__ == "__main__":
    main()
