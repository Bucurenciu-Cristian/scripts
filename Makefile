.PHONY: help install run clean requirements

help:
	@echo "Available commands:"
	@echo "  make install     - Install dependencies with uv"
	@echo "  make run         - Run the neptun booking script with uvx"
	@echo "  make clean       - Remove uv lock files and cache"
	@echo "  make requirements - Update requirements.txt"
	@echo "  make help        - Show this help message"

install:
	uv add selenium
	@echo "Dependencies installed with uv"
	@echo "Note: Make sure you have Chrome browser and ChromeDriver installed"

run:
	uvx --from selenium python neptun.py

clean:
	rm -f uv.lock
	uv cache clean
	@echo "UV cache and lock files cleaned"

requirements:
	@echo "selenium>=4.0.0" > requirements.txt
	@echo "requirements.txt updated"