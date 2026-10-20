PYTHON ?= python3

.PHONY: check run plain test

check:
	$(PYTHON) -m py_compile pith.py

run:
	$(PYTHON) pith.py

plain:
	$(PYTHON) pith.py --plain

test: check
	$(PYTHON) -m unittest discover -s tests -v
