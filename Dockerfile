FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv sync --no-dev --frozen

ENV PATH="/app/.venv/bin:$PATH"

RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

ENV KOHA_TRIAGE_DB_PATH=/data/koha-triage.db

VOLUME /data
EXPOSE 8000

ENTRYPOINT ["koha-triage"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
