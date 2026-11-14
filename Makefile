LIVE := claude codex pi opencode demo

.PHONY: help fmt lint types test js validate hooks static check $(LIVE:%=live-%)

SUITES := tests $(patsubst %/,%,$(wildcard plugins/*/tests/))
PLUGINS := $(patsubst %/,%,$(wildcard plugins/*/))
JS_TESTS := $(wildcard plugins/*/adapters/*/*.test.ts plugins/*/tests/ui/*.test.js)

# xdist workers per suite; `make test TEST_WORKERS=0` runs every suite in one process, for readable failures.
TEST_WORKERS ?= 4
test/tests: TEST_WORKERS = 2

help:
	@echo "Targets:"
	@echo "  fmt     Format and auto-fix Python"
	@echo "  lint    ruff check and ruff format --check"
	@echo "  types   mypy --strict on tools, tests and plugin sources"
	@echo "  test    pytest on the repo tests and each plugin, side by side, each in its own process (live_* excluded)"
	@echo "  js      node --test on the TS adapter tests"
	@echo "  validate   claude plugin validate --strict on the marketplace and every plugin"
	@echo "  hooks   pre-commit on all tracked and untracked files"
	@echo "  static  lint + types + js + validate + hooks, in parallel"
	@echo "  check   static, then test"
	@echo "  live-<harness>  live tests against the real harness: $(LIVE)"

fmt:
	ruff format .
	ruff check --fix .

lint:
	ruff check .
	ruff format --check .

# Each plugin is checked on its own: test and script module names repeat across plugins.
# Separate caches let the runs go side by side; mypy refuses a folder without Python files.
types: types/tools $(PLUGINS:%=types/%)

types/tools:
	mypy --cache-dir .mypy_cache/tools tools tests

types/plugins/%:
	@plugin=plugins/$*; [ -d "$$plugin/src" ] || exit 0; extra=; \
	for dir in scripts bin; do \
	  find "$$plugin/$$dir" -maxdepth 1 -name '*.py' 2>/dev/null | grep -q . && extra="$$extra $$plugin/$$dir"; \
	done; \
	MYPYPATH="$$plugin/tests:$$plugin/scripts" mypy --cache-dir .mypy_cache/$* "$$plugin/src" "$$plugin/tests" $$extra

# One process per suite: each has its own conftest, and `from conftest import` takes the first one loaded.
# Each suite keeps its own cache, so suites running side by side never overwrite each other's lastfailed.
test:
	$(MAKE) -j -O $(SUITES:%=test/%)

test/%:
	pytest -q -n $(TEST_WORKERS) -o cache_dir=.pytest_cache/$(subst /,_,$*) $*

js:
ifeq ($(strip $(JS_TESTS)),)
	@echo "no TS tests"
else
	node --test --test-timeout=60000 $(JS_TESTS)
endif

validate: validate/. $(PLUGINS:%=validate/%)

validate/%:
	claude plugin validate $* --strict

# Untracked files count too, so a tree with nothing committed or staged isn't a false green.
hooks:
	git ls-files -z -co --exclude-standard | xargs -0 -r pre-commit run --files

static:
	$(MAKE) -j -O lint types js validate hooks

check: static test
	@echo ""
	@echo "All checks passed."

# A static pattern rule, because make skips implicit rule search for phony targets.
$(LIVE:%=live-%): live-%:
	pytest -m live_$* plugins/mz-relay/tests/live -p no:xdist -o addopts=""
