FROM python:3.11-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev
COPY remediator remediator
COPY alembic alembic
COPY alembic.ini .
COPY scripts scripts
COPY fixtures fixtures
COPY probes probes
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "remediator.api:app", "--host", "0.0.0.0", "--port", "8000"]
