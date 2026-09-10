# Primary distribution per PRD §8. Build from the repo root:
#   docker build -f docker/Dockerfile -t mongopg-migrate:latest .
#
# Published automatically on a version tag — see .github/workflows/release.yml
# and the GHCR pull instructions in README.md.
FROM python:3.12-slim

# Build metadata, filled in by the release workflow. Declared with defaults so
# a plain local `docker build` still works with no --build-arg.
ARG VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="mongopg-migrate" \
      org.opencontainers.image.description="Map MongoDB collections onto an existing PostgreSQL schema and run a validated, repeatable migration." \
      org.opencontainers.image.source="https://github.com/aggtushar123/mongopg-migrate" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

WORKDIR /app

# Copy only what the build needs, so editing a test or a doc does not bust
# the dependency layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Don't run a tool that writes to production databases as root. Both commands
# are on PATH from the package's entry points, so no PATH juggling is needed.
RUN useradd --create-home --uid 10001 migrate \
    && mkdir -p /work && chown migrate:migrate /work

# A mapping file is the one input every command needs; mount it here:
#   docker run --rm -v "$PWD/mapping.yaml:/work/mapping.yaml:ro" ... \
#     ghcr.io/aggtushar123/mongopg-migrate:latest dry-run /work/mapping.yaml ...
#
# The fan-in helper is a second entry point in the same image:
#   docker run --rm --entrypoint mongopg-fanin ghcr.io/.../mongopg-migrate:latest --help
WORKDIR /work
USER migrate

ENTRYPOINT ["mongopg-migrate"]
CMD ["--help"]
