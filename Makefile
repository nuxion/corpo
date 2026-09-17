define USAGE
Super awesome hand-crafted build system ⚙️

Commands:
	setup     Install dependencies, dev included
	lock      Generate requirements.txt
	test      Run tests
	lint      Run linting tests
	run       Run docker image with --rm flag but mounted dirs.
	release   Publish docker image based on some variables
	docker    Build the docker image
	tag    	  Make a git tab using poetry information
	zipapps   Build standalone `board` and `pipeline` executables with shiv

endef

export USAGE
.EXPORT_ALL_VARIABLES:
GIT_TAG := $(shell git describe --tags)
BUILD := $(shell git rev-parse --short HEAD)
VERSION := $(shell uv version --short)
PROJECTNAME := $(shell basename "$(PWD)")
DOCKERID = $(shell echo "nuxion")

help:
	@echo "$$USAGE"

clean:
	find . ! -path "./.eggs/*" -name "*.pyc" -exec rm {} \;
	find . ! -path "./.eggs/*" -name "*.pyo" -exec rm {} \;
	find . ! -path "./.eggs/*" -name ".coverage" -exec rm {} \;
	rm -rf build/* > /dev/null 2>&1
	rm -rf dist/* > /dev/null 2>&1
	rm -rf .ipynb_checkpoints/* > /dev/null 2>&1
	rm -rf docker/client/dist
	rm -rf docker/all/dist

lock:
	uv export --no-dev --format requirements-txt > requirements.txt

lock-extra:
	# as example, replace extra with the realname
	hatch run pip-compile --extra extra -o requirements.extra.txt  pyproject.toml

lint:
	# pylint --disable=R,C,W services --ignore-paths=services/files
	ruff check

check:
	mypy -p services --exclude services.files

black:
	black services tests

isort:
	isort services tests --profile=black

format: isort black

.PHONY: test
test:
	PYTHONPATH=$(PWD) pytest --cov-report xml --cov=labfunctions tests/

.PHONY: docs-server
docs-serve:
	hatch run sphinx-autobuild docs/source docs/build/html --port 9292 --watch ./

## Standalone single-file executables (shiv)

SHIV_PYTHON ?= /usr/bin/env python3
SHIV_OUT ?= dist

.PHONY: zipapps ado-zipapp
zipapps: ado-zipapp

ado-zipapp:
	mkdir -p $(SHIV_OUT)
	uv run shiv -c ado -o $(SHIV_OUT)/ado -p "$(SHIV_PYTHON)" --compressed .

install-zipapps: zipapps
	mkdir -p $(HOME)/.local/bin
	install -m 0755 $(SHIV_OUT)/ado $(HOME)/.local/bin/ado

## Standard commands for CI/CD cycle

deploy:
	echo "Not implemented"

build-local:
	docker build . -t ${DOCKERID}/${PROJECTNAME}
	docker tag ${DOCKERID}/${PROJECTNAME} ${DOCKERID}/${PROJECTNAME}:${VERSION}

build:
	echo "Not implemented"

publish:
	echo "Not implemented"

