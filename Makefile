.PHONY: install test lint typecheck migrate run docker-up docker-down smoke

install:
	python -m pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check app

typecheck:
	mypy app

migrate:
	alembic -c migrations/alembic.ini upgrade head

run:
	python -m app.main

docker-up:
	docker compose up --build

docker-down:
	docker compose down

smoke:
	docker compose up --build -d postgres migrate bot
	curl -fsS http://localhost:8000/ready

