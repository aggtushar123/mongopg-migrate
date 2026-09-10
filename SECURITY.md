# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/aggtushar123/mongopg-migrate/security/advisories/new)
rather than opening a public issue.

Expect an acknowledgement within a week. If a fix is warranted, the advisory
and a patched release are published together.

## What this tool touches

`mongopg-migrate` reads a MongoDB database and writes to a PostgreSQL one. It
is normally pointed at production data, so the notes below are about handling
that responsibly rather than about a network-facing attack surface.

- **Credentials are never written to disk by this tool.** Connection strings
  come from `--mongo-uri` / `--postgres-uri` or the `MONGO_URI` /
  `POSTGRES_URI` environment variables, and `external_databases:` in a mapping
  file names an *environment variable*, never a credential — so a mapping file
  is safe to commit.
- **Passwords are masked in output.** Any URI echoed back in an error or a
  confirmation prompt has its password replaced. If you find a path that
  prints one, that is a bug worth reporting.
- **`--mode truncate` deletes data.** It empties every table the mapping
  writes to. It asks first, and `--yes` skips that prompt; treat `--yes` in a
  script the way you would treat `DROP TABLE`.
- **The tool needs DDL rights** on the target, to create its own schema
  (`_mongopg` by default, see `--internal-schema`) holding the id map and
  checkpoints.
- **`propose --llm` sends schema metadata to a third party** — collection and
  field names, inferred types and shapes. It never sends row data. It is off
  by default and must be asked for explicitly.
- **Mapping files are executable configuration** in the sense that they decide
  what is written where. Review one from an untrusted source as you would a
  migration script.

## Supported versions

Pre-1.0: only the latest release receives fixes.
