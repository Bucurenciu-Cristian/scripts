.PHONY: help install run run-headless status delete collect collect-verbose db-status db-availability db-clean clean requirements

help:
	@echo "Neptune Sauna Booking Script"
	@echo "============================="
	@echo ""
	@echo "Interactive Booking:"
	@echo "  make run            - Run booking wizard (windowed)"
	@echo "  make run-headless   - Run booking wizard (headless)"
	@echo ""
	@echo "Appointment Management:"
	@echo "  make status         - View current appointments"
	@echo "  make delete         - Delete appointments interactively"
	@echo ""
	@echo "Data Collection (for cron):"
	@echo "  make collect        - Collect availability for all subscriptions"
	@echo "  make collect-verbose - Collect with verbose output"
	@echo ""
	@echo "Database:"
	@echo "  make db-status      - Show database statistics"
	@echo "  make db-availability - Show recent availability data"
	@echo "  make db-clean       - Remove database (careful!)"
	@echo ""
	@echo "Setup:"
	@echo "  make install        - Install dependencies with uv"
	@echo "  make clean          - Clean caches and temp files"
	@echo "  make requirements   - Update requirements.txt"

install:
	uv venv
	uv pip install -r requirements.txt
	@echo "Dependencies installed with uv"
	@echo "Note: Make sure you have Chrome browser and ChromeDriver installed"

# Interactive booking
run:
	uv run python neptun.py

run-headless:
	uv run python neptun.py --headless

# Appointment management
status:
	uv run python neptun.py --status --headless

delete:
	uv run python neptun.py --delete --headless

# Data collection (for cron jobs)
collect:
	uv run python neptun.py --collect

collect-verbose:
	uv run python neptun.py --collect --verbose

# Database management
db-status:
	@echo "Database Statistics:"
	@echo "===================="
	@sqlite3 neptun.db "SELECT 'Sessions: ' || COUNT(*) FROM sessions;" 2>/dev/null || echo "No database found"
	@sqlite3 neptun.db "SELECT 'Availability records: ' || COUNT(*) FROM availability;" 2>/dev/null || true
	@sqlite3 neptun.db "SELECT 'Booking attempts: ' || COUNT(*) FROM booking_attempts;" 2>/dev/null || true
	@sqlite3 neptun.db "SELECT 'Errors: ' || COUNT(*) FROM audit_log WHERE success=0;" 2>/dev/null || true

db-availability:
	@echo "Recent Availability Data:"
	@echo "========================="
	@sqlite3 -header -column neptun.db \
		"SELECT date, time_slot, spots_available, subscription_code, timestamp \
		FROM availability \
		WHERE date >= date('now') \
		ORDER BY date, time_slot \
		LIMIT 20;" 2>/dev/null || echo "No data found"

db-clean:
	rm -f neptun.db
	rm -rf screenshots/
	@echo "Database and screenshots removed"

# Cleanup
clean:
	rm -f uv.lock
	uv cache clean
	rm -rf __pycache__/
	rm -rf .ropeproject/
	@echo "UV cache and temp files cleaned"

requirements:
	@echo "selenium>=4.0.0" > requirements.txt
	@echo "requirements.txt updated"
