# Primary distribution per PRD §8. Build from the repo root:
#   docker build -f docker/Dockerfile -t mongopg-migrate:latest .
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENTRYPOINT ["mongopg-migrate"]
CMD ["--help"]
