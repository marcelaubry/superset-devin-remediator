.PHONY: fmt lint typecheck test up migrate simulate readiness readiness-mutating audit
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
# Dependency, image and secret-hygiene audits (docker required for the image/gitleaks scans)
audit:
	uv export --no-dev --no-hashes -q -o /tmp/remediator-requirements.txt
	uvx pip-audit -r /tmp/remediator-requirements.txt --progress-spinner off
	docker build -q -f docker/verifier/Dockerfile -t remediator-verifier:audit .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.58.1 image \
	  --severity HIGH,CRITICAL --scanners vuln,secret --ignore-unfixed remediator-verifier:audit
	docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:v8.21.2 git /repo --no-banner --redact
