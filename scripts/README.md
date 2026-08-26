# scripts/

Standalone helpers that live outside `src/mongopg_migrate` on purpose —
each one does something the core tool's mapping DSL deliberately does not
(see the PRD's non-goals, §4), so it isn't part of `mongopg-migrate` itself.

## `fanin_reshape.py` — Stage 0 for fan-in (N docs → 1 row)

None of the mapping DSL's constructs (`fields`, `explode`, `junction`,
`unpivot`) can express "N Mongo documents collapse into 1 target row" —
e.g. "the latest `KycVerificationStep` document per booking". That's
grouping/aggregation, a different shape of work from field mapping
(PRD §4 non-goal).

This script runs the standard Mongo aggregation pattern for that reshape —
group, pick one document per group, write to a new collection — so the
*result* is an already one-to-one-shaped collection you point
`mongopg-migrate` at normally:

```bash
# Preview first — writes nothing:
python scripts/fanin_reshape.py \
  --mongo-uri mongodb://localhost:27017/app --database app \
  --source kycVerificationSteps --dest kycVerificationSteps_latest \
  --group-by bookingId --pick-latest-by updatedAt \
  --mode out --dry-run

# Then actually write:
python scripts/fanin_reshape.py \
  --mongo-uri mongodb://localhost:27017/app --database app \
  --source kycVerificationSteps --dest kycVerificationSteps_latest \
  --group-by bookingId --pick-latest-by updatedAt \
  --mode out

# Now map kycVerificationSteps_latest with mongopg-migrate as usual —
# introspect / propose / dry-run / migrate / validate.
```

`--mode out` replaces `--dest`'s entire contents every run (simplest,
right for a one-time migration). `--mode merge` upserts by `--merge-on`
(default: the same fields as `--group-by`) instead, leaving unrelated
existing `--dest` documents alone — useful if you're re-running this
periodically — but Mongo requires a **pre-existing unique index** on
`--dest` covering the merge key, or the write is rejected with a clear
error naming the missing index.

For grouping logic beyond "pick by max/min of one field" (e.g. "prefer
`status=APPROVED` over `PENDING`, then latest"), supply your own pipeline
stages via `--pipeline-file` (a JSON array, through `$group`/`$replaceRoot`
— this script still adds the `$match`/`--filter` in front and the
`$out`/`$merge` stage at the end, so the same dry-run/confirmation/count
report applies either way).

**Caveat, stated once here and again by the script itself:** once you
migrate the derived collection, `mongopg-migrate validate`'s count/sample
diff checks the derived collection against Postgres, not the original
source collection. That moves the verification boundary — the N→1
reduction this script performs is never re-checked by anything
downstream of it. Look at `--dry-run`'s preview before committing to a
write.

See `python scripts/fanin_reshape.py --help` for the full flag reference,
and `tests/test_fanin_reshape.py` for behavior (the `$merge` write path
itself is live-Docker-verified rather than unit-tested — `mongomock`
doesn't implement `$merge`).
