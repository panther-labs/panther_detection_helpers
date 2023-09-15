packages = panther_detection_helpers


deps:
	pipenv install --dev

deps-update:
	pipenv update
	pipenv lock
	pipenv requirements > requirements.txt

lint:
	pipenv run mypy $(packages) --disallow-untyped-defs --ignore-missing-imports --warn-unused-ignores
	pipenv run bandit -r $(packages)
	pipenv run pylint $(packages) --disable=missing-docstring,duplicate-code,C0209,W0511,R0912,too-many-lines,too-many-instance-attributes --max-line-length=140

fmt:
	pipenv run isort --profile=black $(packages)
	pipenv run black --line-length=100 $(packages)

install:
	pipenv install --dev
	pipenv requirements > requirements.txt

install-pipenv:
	pip install pipenv

package-clean:
	rm -rf dist
	rm -f MANIFEST
	rm -rf *.egg-info

package: package-clean install test lint
	pipenv run python3 setup.py sdist

publish: install package
	twine upload dist/*

test:
	pipenv run nosetests -v
