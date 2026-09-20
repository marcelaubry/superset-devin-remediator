.PHONY: fmt lint typecheck test up migrate simulate readiness readiness-mutating
fmt:
	uv run ruff format .
lint:
	uv run ruff check .
typecheck:
	uv run mypy remediator
test:
	uv run pytest -q
up:
	docker compose up --build
migrate:
	uv run alembic upgrade head
simulate:
	uv run python scripts/simulate.py --scenario all --wait
readiness:
	uv run python -m remediator.readiness
readiness-mutating:
	uv run python -m remediator.readiness --allow-mutations
