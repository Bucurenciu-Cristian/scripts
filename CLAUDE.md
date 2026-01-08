# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

Neptune is a Python web automation script for booking sauna reservations on the BPSB system. The script uses Selenium WebDriver to interact with the booking website, featuring:
- Complete Romanian user interface
- CSV-based configuration
- Support for couple/group bookings
- **Layered reliability with selector fallbacks**
- **SQLite-based logging and data collection**
- **Silent data collection mode for cron jobs**
- **Appointment management (view/delete)**
- **Post-booking verification with login**

## File Structure

```
Script Neptun/
├── neptun.py          # Main automation script (~2500 lines)
├── neptun.db          # SQLite database (auto-created)
├── screenshots/       # Error screenshots (auto-created)
├── input.csv          # Subscription codes configuration
├── .env               # Login credentials (not committed)
├── .env.example       # Credentials template
├── Makefile           # Build and run commands
├── requirements.txt   # Python dependencies
├── README.md          # Project documentation
└── CLAUDE.md          # AI assistant instructions
```

## Dependencies and Setup

**Requirements:**
- Python 3.x with Selenium
- Chrome browser with ChromeDriver installed
- UV package manager (recommended)

**Running the script:**
```bash
# Interactive booking
make run              # Windowed mode
make run-headless     # Headless mode

# Appointment management (requires .env credentials)
make status           # View current appointments
make delete           # Delete appointments interactively

# Data collection (for cron)
make collect          # Collect all subscriptions
make collect-verbose  # With verbose output

# Database management
make db-status        # Show statistics
make db-availability  # Show recent data
make db-clean         # Remove database
```

## Architecture and Code Structure

### Core Classes (Reliability Infrastructure)

**SelectorRegistry**
- Centralized selector management with fallback chains
- Each element has: CSS (primary), XPath (fallback), text-based (last resort)
- Legacy absolute XPaths preserved as comments for reference

**ElementFinder**
- Robust element location with automatic fallback
- Built-in retry logic with exponential backoff
- Logs which selector method succeeded

**DatabaseManager**
- SQLite operations for logging and data collection
- Tables: `sessions`, `availability`, `booking_attempts`, `audit_log`, `error_details`
- Auto-creates database and schema on first run

**NeptunLogger**
- Dual logging: Romanian console messages, English database entries
- Tracks action counts and error counts per session

**StateVerifier**
- Verifies actions completed successfully
- Captures screenshots on errors
- Validates page state at checkpoints

**AvailabilityCollector**
- Silent `--collect` mode for cron jobs
- Scrapes all dates and slots
- Logs availability data to SQLite

### Exit Codes

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | SUCCESS | Operation completed |
| 1 | INVALID_SUBSCRIPTION | Bad subscription code |
| 2 | NO_AVAILABILITY | No slots available |
| 3 | BOOKING_FAILED | Booking operation failed |
| 4 | NETWORK_ERROR | Connection issues |
| 5 | ELEMENT_NOT_FOUND | Page structure changed |
| 6 | TIMEOUT | Operation timed out |
| 99 | UNKNOWN_ERROR | Unexpected error |

### Helper Functions (Legacy)

**Configuration Management**
- `choose_subscription_code()` - loads subscription codes from `input.csv`
- `load_subscription_codes()` - returns list of codes for collection mode
- `create_browser_options()` - configures Chrome for headless/windowed

**Slot Management**
- `parse_slot_info()` - extracts availability from slot elements
- `get_available_timeslots()` - finds all available booking slots
- `select_multiple_slots()` - handles user selection with duplicate support
- `process_slot_selection()` - processes individual slot booking

**Calendar Navigation**
- `check_and_navigate_calendar()` - navigates to months with sufficient availability
- `get_available_dates()` - extracts available dates from calendar table
- `count_available_dates()` - counts available dates in current view

**Validation**
- `validate_slot_selections()` - ensures selections meet all constraints
- `validate_quantity()` - validates requested quantity against limits
- `get_max_reservations()` / `get_remaining_reservations()` - extracts booking limits

## Selector Strategy

The `SelectorRegistry` class centralizes all selectors with fallback chains:

```python
"subscription_input": {
    "css": "form div input[type='text']",           # Primary
    "xpath": "//form//input[@type='text']",         # Fallback
    "text": None,                                    # Text-based (if applicable)
    # Legacy: /html/body/div[1]/div/.../form/div/input
}
```

When a CSS selector fails, the system automatically tries XPath, then text-based matching.

## SQLite Schema

```sql
-- Availability snapshots (for --collect mode)
availability(id, timestamp, subscription_code, date, time_slot, spots_available, session_id)

-- Booking attempts log
booking_attempts(id, timestamp, subscription_code, date, time_slot, success, error_message)

-- Audit trail for all actions
audit_log(id, timestamp, action_type, element_name, selector_method, success, duration_ms)
```

## CLI Arguments

```
python neptun.py [options]

Options:
  --headless        Run without browser window
  --status          View current appointments (requires .env credentials)
  --delete          Delete appointments interactively (requires .env credentials)
  --collect         Silent data collection mode (implies --headless)
  --all             Collect for all subscriptions
  -s, --subscription CODE   Use specific subscription code
  -v, --verbose     Verbose output
  --db FILE         Custom database path
```

## Booking Flow

1. **Code Selection**: Dynamic display from CSV with fallback
2. **Subscription Validation**: Error detection with state verification
3. **Reservation Management**: Capacity checking with ElementFinder
4. **Date Selection**: Calendar navigation with retry logic
5. **Slot Selection**: Multiple selection with duplicate support
6. **Processing**: Slot booking with stale element recovery
7. **Completion**: Success confirmation with database logging

## Data Collection Flow (--collect mode)

1. Load subscription codes from `input.csv`
2. For each subscription:
   - Navigate to booking page
   - Enter subscription code
   - Verify subscription is valid
   - Extract available dates
   - For each date, extract slot availability
   - Log all data to SQLite
3. Return exit code for cron

## Makefile Commands

```bash
# Interactive Booking
make run              # Run booking wizard (windowed)
make run-headless     # Run booking wizard (headless)

# Appointment Management
make status           # View current appointments (headless)
make delete           # Delete appointments interactively (headless)

# Data Collection
make collect          # Collect availability for all subscriptions
make collect-verbose  # Collect with verbose output

# Database Management
make db-status        # Show database statistics
make db-availability  # Show recent availability data
make db-clean         # Remove database

# Setup
make install          # Install dependencies with UV
make clean            # Clean caches and temp files
```

## Error Handling Patterns

**Selector Fallbacks**: CSS → XPath → text-based, with logging of which method succeeded

**Retry with Backoff**: `@with_retry` decorator provides exponential backoff for flaky operations

**Stale Element Recovery**: Automatic fresh element fetching when DOM changes

**State Verification**: Checkpoints verify expected state before proceeding

**Screenshot on Error**: Captures page state for debugging

## Cron Job Setup

```bash
# Example crontab entry (every 2 hours)
0 */2 * * * cd /path/to/Script\ Neptun && make collect >> /var/log/neptun.log 2>&1

# Check exit code for alerting
make collect; echo "Exit code: $?"
```

## Configuration

### Subscription Codes (`input.csv`)
```csv
code,name
5642ece785,Kicky
3adc06c0e8,Adrian
```

Add new users by appending rows to this file.

## Future Extensibility

- **Notification interface**: Designed for Telegram/Pushover (not implemented)
- **Config file support**: Can add `neptun.yaml` for complex configuration
- **Preferred time filtering**: Can add to collector mode
