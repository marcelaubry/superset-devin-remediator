.PHONY: fmt lint typecheck test up migrate simulate visual readiness readiness-smoke readiness-mutating audit
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
readiness:
	uv run python -m remediator.readiness
readiness-smoke:
	uv run python -m remediator.readiness --verifier-smoke
readiness-mutating:
	@test -n "$(CONFIRM_CHANNEL)" || (echo "usage: make readiness-mutating CONFIRM_CHANNEL=<SLACK_CHANNEL_ID>" && exit 2)
	uv run python -m remediator.readiness --allow-mutations --confirm-channel "$(CONFIRM_CHANNEL)"
# Dependency, image and secret-hygiene audits (docker required for the image/gitleaks scans)
audit:
	uv export --no-dev --no-hashes -q -o /tmp/remediator-requirements.txt
	uvx pip-audit -r /tmp/remediator-requirements.txt --progress-spinner off
	docker build -q -f docker/verifier/Dockerfile -t remediator-verifier:audit .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.58.1 image \
	  --severity HIGH,CRITICAL --scanners vuln,secret --ignore-unfixed remediator-verifier:audit
	docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:v8.21.2 git /repo --no-banner --redact
