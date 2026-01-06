PYTHON?=python
MANAGE?=$(PYTHON) manage.py

run:
	$(MANAGE) runserver

lint:
	ruff tracker assistant || true
	isort --check --diff tracker assistant || true
	black --check tracker assistant || true

test:
	pytest

import-demo:
	$(MANAGE) import_legacy
