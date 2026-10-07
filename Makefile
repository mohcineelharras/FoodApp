PYTHONPATH := src
export PYTHONPATH

.PHONY: test lint audit run

test:
	pytest

lint:
	ruff check src tests

audit:
	pip-audit -r requirements.txt -r requirements-dev.txt

run:
	python -m foodapp
