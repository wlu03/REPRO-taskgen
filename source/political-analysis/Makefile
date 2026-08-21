.PHONY: test smoke inventory help

test:
	python3 -m unittest discover -s tests -v

smoke:
	./run_scraper.sh --smoke-test

inventory:
	./run_scraper.sh --inventory-only

help:
	./run_scraper.sh --help
