.PHONY: dev test lint fmt typecheck

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
