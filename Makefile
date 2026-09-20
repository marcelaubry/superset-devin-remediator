.PHONY: fmt lint typecheck test up migrate simulate visual
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
# Reproducible screenshots + structural a11y checks (artifacts/visual). The simulator needs a
# clean database (one approval decision per case), so the local compose volume is reset first.
visual:
	mkdir -p artifacts/visual
	docker compose -f docker-compose.yml -f docker-compose.visual.yml down -v --remove-orphans
	docker compose -f docker-compose.yml -f docker-compose.visual.yml build
	docker compose -f docker-compose.yml -f docker-compose.visual.yml run --rm visual
