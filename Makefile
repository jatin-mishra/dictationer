.PHONY: dev serve test lint fmt typecheck

dev:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff check --select I --fix . && uv run ruff format .

typecheck:
	uv run mypy app

serve:  # LAN-reachable for the mobile app; single worker (file locks + one MLX model)
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
