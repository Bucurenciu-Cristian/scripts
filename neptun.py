from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
import time
from datetime import datetime, timedelta
import argparse
import csv
import sqlite3
import uuid
import os
import functools
import sys
import re
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

# Database configuration
DB_FILE = "neptun.db"
SCREENSHOTS_DIR = "screenshots"

# Timeout configuration (seconds)
TIMEOUT_SHORT = 3
TIMEOUT_MEDIUM = 10
TIMEOUT_LONG = 30

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds (base delay, will use exponential backoff)

# Exit codes for cron/systemd
class ExitCode:
    SUCCESS = 0
    INVALID_SUBSCRIPTION = 1
    NO_AVAILABILITY = 2
    BOOKING_FAILED = 3
    NETWORK_ERROR = 4
    ELEMENT_NOT_FOUND = 5
    TIMEOUT = 6
    UNKNOWN_ERROR = 99


class TimingConfig:
    """
    Timing configuration for different operation modes.
    Aggressive timings for collection, conservative for interactive.
    """
    INTERACTIVE = {
        'page_load': 1.0,
        'subscription_input': 0.5,
        'sauna_click': 2.0,
        'calendar_nav': 0.5,
        'next_month': 0.5,
        'date_click': 0.5,
        'back_reload': 1.0,
    }

    COLLECT = {
        'page_load': 0.3,
        'subscription_input': 0.15,
        'sauna_click': 0.5,
        'calendar_nav': 0.15,
        'next_month': 0.15,
        'date_click': 0.15,
        'back_reload': 0.3,
    }

    @classmethod
    def get(cls, mode='interactive'):
        """Get timing config for mode. mode='collect' for fast, else conservative."""
        return cls.COLLECT if mode == 'collect' else cls.INTERACTIVE


# =============================================================================
# CREDENTIALS LOADING
# =============================================================================

def get_credentials():
    """
    Load login credentials from environment variables.
    
    Expects .env file with:
        NEPTUN_EMAIL=your@email.com
        NEPTUN_PASSWORD=yourpassword
    
    Returns:
        dict with 'email' and 'password' keys, or None values if not set
    """
    return {
        'email': os.getenv('NEPTUN_EMAIL'),
        'password': os.getenv('NEPTUN_PASSWORD')
    }


def has_credentials():
    """Check if login credentials are configured."""
    creds = get_credentials()
    return creds['email'] is not None and creds['password'] is not None


# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================

class NeptunError(Exception):
    """Base exception for Neptune errors."""
    exit_code = ExitCode.UNKNOWN_ERROR


class ElementNotFoundError(NeptunError):
    """Raised when an element cannot be found with any selector method."""
    exit_code = ExitCode.ELEMENT_NOT_FOUND

    def __init__(self, element_name, attempts=None):
        self.element_name = element_name
        self.attempts = attempts or []
        super().__init__(f"Element '{element_name}' not found after trying: {attempts}")


class InvalidSubscriptionError(NeptunError):
    """Raised when subscription code is invalid or expired."""
    exit_code = ExitCode.INVALID_SUBSCRIPTION


class BookingError(NeptunError):
    """Raised when booking operation fails."""
    exit_code = ExitCode.BOOKING_FAILED


# =============================================================================
# DATABASE MANAGER
# =============================================================================

class DatabaseManager:
    """
    SQLite database management for logging and data collection.
    """

    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self.conn = None
        self._initialize_db()

    def _initialize_db(self):
        """Create tables if they don't exist."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

        cursor = self.conn.cursor()

        # Sessions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                start_time TEXT NOT NULL,
                end_time TEXT,
                mode TEXT NOT NULL,
                exit_code INTEGER,
                subscription_codes TEXT,
                total_actions INTEGER DEFAULT 0,
                total_errors INTEGER DEFAULT 0
            )
        ''')

        # Availability snapshots (for --collect mode)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                subscription_code TEXT NOT NULL,
                subscription_name TEXT,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                spots_available INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        ''')

        # Booking attempts log
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS booking_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT NOT NULL,
                subscription_code TEXT NOT NULL,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                requested_spots INTEGER NOT NULL,
                success INTEGER NOT NULL,
                error_code INTEGER,
                error_message TEXT,
                duration_ms INTEGER,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        ''')

        # Audit trail for all actions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                element_name TEXT,
                selector_method TEXT,
                success INTEGER NOT NULL,
                duration_ms INTEGER,
                details TEXT,
                screenshot_path TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        ''')

        # Error details (linked to audit_log)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS error_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_log_id INTEGER NOT NULL,
                error_type TEXT NOT NULL,
                error_message TEXT,
                page_url TEXT,
                page_title TEXT,
                screenshot_path TEXT,
                FOREIGN KEY (audit_log_id) REFERENCES audit_log(id)
            )
        ''')

        # Create indexes for faster queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_availability_date ON availability(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_availability_subscription ON availability(subscription_code)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id)')

        self.conn.commit()

    def create_session(self, mode, subscription_codes=None):
        """Create a new session and return its ID."""
        session_id = str(uuid.uuid4())[:8]
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO sessions (id, start_time, mode, subscription_codes)
            VALUES (?, datetime('now'), ?, ?)
        ''', (session_id, mode, str(subscription_codes) if subscription_codes else None))
        self.conn.commit()
        return session_id

    def end_session(self, session_id, exit_code, total_actions=0, total_errors=0):
        """Mark session as ended with final stats."""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE sessions
            SET end_time = datetime('now'), exit_code = ?, total_actions = ?, total_errors = ?
            WHERE id = ?
        ''', (exit_code, total_actions, total_errors, session_id))
        self.conn.commit()

    def log_availability(self, session_id, subscription_code, date, time_slot, spots_available, subscription_name=None):
        """Log availability data for a specific slot."""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO availability (session_id, subscription_code, subscription_name, date, time_slot, spots_available)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session_id, subscription_code, subscription_name, date, time_slot, spots_available))
        self.conn.commit()

    def log_booking_attempt(self, session_id, subscription_code, date, time_slot, requested_spots, success, error_code=None, error_message=None, duration_ms=None):
        """Log a booking attempt."""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO booking_attempts (session_id, subscription_code, date, time_slot, requested_spots, success, error_code, error_message, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (session_id, subscription_code, date, time_slot, requested_spots, 1 if success else 0, error_code, error_message, duration_ms))
        self.conn.commit()

    def log_action(self, session_id, action_type, element_name=None, selector_method=None, success=True, duration_ms=None, details=None, screenshot_path=None):
        """Log an action to the audit trail."""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO audit_log (session_id, action_type, element_name, selector_method, success, duration_ms, details, screenshot_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (session_id, action_type, element_name, selector_method, 1 if success else 0, duration_ms, details, screenshot_path))
        self.conn.commit()
        return cursor.lastrowid

    def log_error(self, audit_log_id, error_type, error_message, page_url=None, page_title=None, screenshot_path=None):
        """Log detailed error information."""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO error_details (audit_log_id, error_type, error_message, page_url, page_title, screenshot_path)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (audit_log_id, error_type, error_message, page_url, page_title, screenshot_path))
        self.conn.commit()

    def get_availability_history(self, days=7):
        """Get availability data for the last N days."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT date, time_slot, spots_available, subscription_code, timestamp
            FROM availability
            WHERE date >= date('now', ? || ' days')
            ORDER BY date, time_slot
        ''', (f'-{days}',))
        return cursor.fetchall()

    def get_booking_stats(self, subscription_code=None):
        """Get booking statistics."""
        cursor = self.conn.cursor()
        if subscription_code:
            cursor.execute('''
                SELECT
                    COUNT(*) as total_attempts,
                    SUM(success) as successful,
                    COUNT(*) - SUM(success) as failed
                FROM booking_attempts
                WHERE subscription_code = ?
            ''', (subscription_code,))
        else:
            cursor.execute('''
                SELECT
                    COUNT(*) as total_attempts,
                    SUM(success) as successful,
                    COUNT(*) - SUM(success) as failed
                FROM booking_attempts
            ''')
        return cursor.fetchone()

    def get_slot_popularity(self, days=30):
        """Get slot popularity by time slot across all dates."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                time_slot,
                COUNT(*) as observations,
                ROUND(AVG(spots_available), 1) as avg_available,
                MIN(spots_available) as min_available,
                MAX(spots_available) as max_available,
                SUM(CASE WHEN spots_available = 0 THEN 1 ELSE 0 END) as fully_booked_count
            FROM availability
            WHERE date >= date('now', ? || ' days')
              AND time_slot LIKE '%:%'
            GROUP BY time_slot
            ORDER BY avg_available ASC
        ''', (f'-{days}',))
        return cursor.fetchall()

    def get_day_of_week_trends(self, days=30):
        """Get availability trends by day of week."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                CASE strftime('%w', date)
                    WHEN '0' THEN 'Duminică'
                    WHEN '1' THEN 'Luni'
                    WHEN '2' THEN 'Marți'
                    WHEN '3' THEN 'Miercuri'
                    WHEN '4' THEN 'Joi'
                    WHEN '5' THEN 'Vineri'
                    WHEN '6' THEN 'Sâmbătă'
                END as day_name,
                strftime('%w', date) as day_num,
                ROUND(AVG(spots_available), 1) as avg_available,
                COUNT(*) as observations
            FROM availability
            WHERE date >= date('now', ? || ' days')
              AND time_slot LIKE '%:%'
            GROUP BY day_num
            ORDER BY day_num
        ''', (f'-{days}',))
        return cursor.fetchall()

    def get_hourly_demand(self, days=30):
        """Get demand by time slot and day combination."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                CASE strftime('%w', date)
                    WHEN '0' THEN 'Dum'
                    WHEN '1' THEN 'Lun'
                    WHEN '2' THEN 'Mar'
                    WHEN '3' THEN 'Mie'
                    WHEN '4' THEN 'Joi'
                    WHEN '5' THEN 'Vin'
                    WHEN '6' THEN 'Sam'
                END as day_short,
                time_slot,
                ROUND(AVG(spots_available), 1) as avg_available,
                SUM(CASE WHEN spots_available = 0 THEN 1 ELSE 0 END) as fully_booked
            FROM availability
            WHERE date >= date('now', ? || ' days')
              AND time_slot LIKE '%:%'
            GROUP BY strftime('%w', date), time_slot
            ORDER BY strftime('%w', date), time_slot
        ''', (f'-{days}',))
        return cursor.fetchall()

    def get_collection_stats(self):
        """Get data collection statistics."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                COUNT(DISTINCT date) as unique_dates,
                COUNT(DISTINCT subscription_code) as unique_subscriptions,
                COUNT(*) as total_records,
                MIN(timestamp) as first_collection,
                MAX(timestamp) as last_collection,
                COUNT(DISTINCT date(timestamp)) as collection_days
            FROM availability
            WHERE time_slot LIKE '%:%'
        ''')
        return cursor.fetchone()

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()


# =============================================================================
# LOGGER
# =============================================================================

class NeptunLogger:
    """
    Structured logging to both console (Romanian) and database (English).
    """

    def __init__(self, db_manager=None, verbose=True):
        self.db = db_manager
        self.verbose = verbose
        self.session_id = None
        self.action_count = 0
        self.error_count = 0

    def set_session(self, session_id):
        """Set the current session ID."""
        self.session_id = session_id

    def info(self, message_ro, message_en=None):
        """Log info level message."""
        if self.verbose:
            print(message_ro)
        if self.db and self.session_id:
            self.db.log_action(self.session_id, 'info', details=message_en or message_ro)
            self.action_count += 1

    def debug(self, message_ro, message_en=None):
        """Log debug level message (only to database)."""
        if self.db and self.session_id:
            self.db.log_action(self.session_id, 'debug', details=message_en or message_ro)

    def warning(self, message_ro, message_en=None):
        """Log warning level message."""
        if self.verbose:
            print(f"⚠️  {message_ro}")
        if self.db and self.session_id:
            self.db.log_action(self.session_id, 'warning', details=message_en or message_ro)
            self.action_count += 1

    def error(self, message_ro, message_en=None, error=None, screenshot_path=None):
        """Log error with full context."""
        print(f"❌ {message_ro}")
        if self.db and self.session_id:
            audit_id = self.db.log_action(
                self.session_id, 'error',
                success=False,
                details=message_en or message_ro,
                screenshot_path=screenshot_path
            )
            if error:
                self.db.log_error(
                    audit_id,
                    type(error).__name__,
                    str(error),
                    screenshot_path=screenshot_path
                )
            self.error_count += 1

    def action(self, action_name, element_name=None, selector_method=None, duration_ms=None, success=True, details=None):
        """Log timed action with performance metrics."""
        if self.db and self.session_id:
            self.db.log_action(
                self.session_id, action_name,
                element_name=element_name,
                selector_method=selector_method,
                success=success,
                duration_ms=duration_ms,
                details=details
            )
            self.action_count += 1
            if not success:
                self.error_count += 1

    def get_stats(self):
        """Return action and error counts."""
        return self.action_count, self.error_count


# =============================================================================
# RETRY DECORATOR
# =============================================================================

def with_retry(max_attempts=MAX_RETRIES, delay=RETRY_DELAY, exceptions=(Exception,)):
    """
    Decorator that retries a function with exponential backoff.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts:
                        wait_time = delay * (2 ** (attempt - 1))  # Exponential backoff
                        time.sleep(wait_time)
                    continue
            raise last_exception
        return wrapper
    return decorator


# =============================================================================
# SELECTOR REGISTRY
# =============================================================================

class SelectorRegistry:
    """
    Centralized selector management with fallback chains.
    Each element has: CSS primary, XPath fallback, text-based last resort.

    Legacy absolute XPaths are preserved as comments for reference if CSS fails.
    """

    SELECTORS = {
        "subscription_input": {
            "css": "input[name='clientInput'], input.form-control-lg[type='text']",
            "xpath": "//input[@name='clientInput']",
            "text": None,
            "description": "Subscription code input field",
        },
        "search_button": {
            "css": "button.btn-primary.btn-lg[type='submit'], .input-group-append button.btn-primary",
            "xpath": "//button[contains(@class, 'btn-primary') and contains(@class, 'btn-lg')]",
            "text": "Cauta",
            "description": "Search/submit button",
        },
        "reservation_count_span": {
            "css": "form button span:nth-child(2), form button span",
            "xpath": "//form/button/span[contains(text(), ':')]",
            "text": "Rezervari disponibile",
            "description": "Reservation count display",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/button/span[2]
        },
        "remaining_reservations_h5": {
            "css": "h5",
            "xpath": "//h5[contains(text(), '/')]",
            "text": None,
            "description": "Remaining reservations header (shows X/Y format)",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/h5
        },
        "sauna_option_button": {
            "css": "button.resource, button.btn-outline-primary.btn-block",
            "xpath": "//button[contains(@class, 'resource')]",
            "text": "Sauna",
            "description": "Sauna service selection button",
        },
        "calendar_table": {
            "css": ".datepicker-days table.table-condensed tbody, .datepicker-days tbody",
            "xpath": "//div[contains(@class, 'datepicker-days')]//table//tbody",
            "text": None,
            "description": "Calendar date picker table body",
        },
        "calendar_header": {
            "css": "th.datepicker-switch, .datepicker-days th.datepicker-switch",
            "xpath": "//th[contains(@class, 'datepicker-switch')]",
            "text": None,
            "description": "Calendar month/year header",
        },
        "next_month_arrow": {
            "css": ".datepicker-days th.next:not(.disabled), th.next:not(.disabled)",
            "xpath": "//div[contains(@class, 'datepicker-days')]//th[contains(@class, 'next') and not(contains(@class, 'disabled'))]",
            "text": None,
            "description": "Next month navigation arrow",
        },
        "time_slot": {
            "css": "div.alert.alert-outline-primary, div.alert-custom.alert-outline-primary",
            "xpath": "//div[contains(@class, 'alert') and contains(@class, 'alert-outline-primary')]",
            "text": "Locuri disponibile",
            "description": "Available time slot element",
        },
        "select_button": {
            "css": "form div div button.btn-primary, form button.btn-primary, form button[type='submit']",
            "xpath": "//form//button[contains(@class, 'btn-primary') or contains(text(), 'Selecteaza')]",
            "text": "Selecteaza",
            "description": "Slot selection confirmation button",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div[4]/div[2]/form/div/div[2]/button
        },
        "slot_select_buttons": {
            "css": "button.select-btn",
            "xpath": "//button[contains(@class, 'select-btn')]",
            "text": "Selecteaza",
            "description": "All Selecteaza buttons for indexed slot selection",
        },
        "cart_close_button": {
            "css": "div.modal a.close, .cart-modal a.close, div > a[aria-label='Close'], div a.close",
            "xpath": "//div[contains(@class, 'modal') or contains(@class, 'cart')]//a[contains(@class, 'close') or @aria-label='Close']",
            "text": None,
            "description": "Cart/modal close button",
            # Legacy: /html/body/div[2]/div[1]/a
        },
        "final_confirm_button": {
            "css": "#rg_summary_next, a.btn-danger[href*='final']",
            "xpath": "//a[@id='rg_summary_next'] | //a[contains(@href, 'final') and contains(@class, 'btn-danger')]",
            "text": "Finalizeaza",
            "description": "Final booking confirmation button",
            # Legacy: /html/body/div[2]/div[2]/div[2]/a[2]
        },
        "error_alert": {
            "css": ".alert-danger, .alert-error, [class*='alert'][class*='danger'], [class*='alert'][class*='error']",
            "xpath": "//*[contains(@class, 'alert') and (contains(@class, 'danger') or contains(@class, 'error'))]",
            "text": "Nu au fost gasite",
            "description": "Error message alert",
        },
        # Login page selectors
        "login_email_input": {
            "css": "#username, input[name='_username']",
            "xpath": "//input[@id='username' or @name='_username']",
            "text": None,
            "description": "Login email/username field",
        },
        "login_password_input": {
            "css": "#password, input[name='_password']",
            "xpath": "//input[@id='password' or @name='_password']",
            "text": None,
            "description": "Login password field",
        },
        "login_submit_button": {
            "css": "#_submit, input[type='submit'][value='Autentificare']",
            "xpath": "//input[@id='_submit' or @value='Autentificare']",
            "text": "Autentificare",
            "description": "Login submit button",
        },
        # Appointments page selectors
        "appointment_table_row": {
            "css": "table.table tbody tr",
            "xpath": "//table[contains(@class, 'table')]//tbody//tr",
            "text": None,
            "description": "Appointment table rows",
        },
        "appointment_delete_button": {
            "css": "button.deleteAppButton",
            "xpath": "//button[contains(@class, 'deleteAppButton')]",
            "text": "Șterge",
            "description": "Delete appointment button",
        },
        "delete_confirm_button": {
            "css": ".swal2-confirm, button.swal2-confirm, .btn-danger[data-dismiss]",
            "xpath": "//button[contains(@class, 'swal2-confirm')] | //button[contains(text(), 'Da') or contains(text(), 'Confirm')]",
            "text": "Da",
            "description": "Confirm delete dialog button",
        },
    }

    @classmethod
    def get(cls, element_name):
        """Get selector configuration for an element."""
        return cls.SELECTORS.get(element_name)

    @classmethod
    def list_elements(cls):
        """List all registered element names."""
        return list(cls.SELECTORS.keys())


# =============================================================================
# ELEMENT FINDER
# =============================================================================

class ElementFinder:
    """
    Robust element location with fallback chain and retry logic.
    """

    def __init__(self, driver, logger=None):
        self.driver = driver
        self.logger = logger
        self.registry = SelectorRegistry

    def find(self, element_name, timeout=TIMEOUT_MEDIUM, required=True):
        """
        Find element using fallback chain: CSS -> XPath -> Text-based.
        Returns (element, method_used) or raises ElementNotFoundError.
        """
        selectors = self.registry.get(element_name)
        if not selectors:
            raise ValueError(f"Unknown element: {element_name}. Available: {self.registry.list_elements()}")

        methods = [
            ("css", By.CSS_SELECTOR, selectors.get("css")),
            ("xpath", By.XPATH, selectors.get("xpath")),
        ]

        # Add text-based fallback if defined
        text = selectors.get("text")
        if text:
            methods.append(("text", By.XPATH, f"//*[contains(text(), '{text}')]"))

        errors = []
        per_method_timeout = max(1, timeout // len(methods))  # Split timeout across methods

        for method_name, by_type, selector in methods:
            if not selector:
                continue
            try:
                start_time = time.time()
                element = WebDriverWait(self.driver, per_method_timeout).until(
                    EC.presence_of_element_located((by_type, selector))
                )
                duration_ms = int((time.time() - start_time) * 1000)

                if self.logger:
                    self.logger.action(
                        'find_element',
                        element_name=element_name,
                        selector_method=method_name,
                        duration_ms=duration_ms,
                        success=True
                    )

                return element, method_name

            except TimeoutException as e:
                errors.append((method_name, f"Timeout after {per_method_timeout}s"))
            except Exception as e:
                errors.append((method_name, str(e)))

        # All methods failed
        if self.logger:
            self.logger.action(
                'find_element',
                element_name=element_name,
                success=False,
                details=f"Failed with: {errors}"
            )

        if required:
            raise ElementNotFoundError(element_name, errors)
        return None, None

    def find_all(self, element_name, timeout=TIMEOUT_MEDIUM):
        """Find all matching elements using fallback chain."""
        selectors = self.registry.get(element_name)
        if not selectors:
            raise ValueError(f"Unknown element: {element_name}")

        methods = [
            ("css", By.CSS_SELECTOR, selectors.get("css")),
            ("xpath", By.XPATH, selectors.get("xpath")),
        ]

        for method_name, by_type, selector in methods:
            if not selector:
                continue
            try:
                # Wait for at least one element to be present
                WebDriverWait(self.driver, timeout // 2).until(
                    EC.presence_of_element_located((by_type, selector))
                )
                elements = self.driver.find_elements(by_type, selector)
                if elements:
                    if self.logger:
                        self.logger.action(
                            'find_all_elements',
                            element_name=element_name,
                            selector_method=method_name,
                            success=True,
                            details=f"Found {len(elements)} elements"
                        )
                    return elements, method_name
            except TimeoutException:
                continue
            except Exception:
                continue

        return [], None

    def click_at_index(self, element_name, index, timeout=TIMEOUT_MEDIUM, retries=MAX_RETRIES):
        """
        Find all elements matching selector, then click the one at specified index.
        
        Args:
            element_name: Name of element in SelectorRegistry
            index: 1-based index (matches user-facing numbering, e.g., slot 1, 2, 3, 4)
            timeout: Wait timeout for finding elements
            retries: Number of retry attempts for stale elements
            
        Returns:
            True if click successful
            
        Raises:
            IndexError: If index is out of range
            Exception: If click fails after all retries
        """
        last_exception = None
        
        for attempt in range(1, retries + 1):
            try:
                elements, method = self.find_all(element_name, timeout=timeout)
                
                if not elements:
                    raise ElementNotFoundError(element_name, [("find_all", "No elements found")])
                
                # Convert 1-based user index to 0-based array index
                zero_index = index - 1
                
                if zero_index < 0 or zero_index >= len(elements):
                    error_msg = f"Index {index} out of range. Found {len(elements)} elements for '{element_name}'."
                    if self.logger:
                        self.logger.error(f"Index invalid: {index}", error_msg)
                    raise IndexError(error_msg)
                
                element = elements[zero_index]
                
                # Scroll element into view
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                time.sleep(0.1)
                
                # Wait briefly for element to be interactive
                WebDriverWait(self.driver, TIMEOUT_SHORT).until(
                    EC.element_to_be_clickable(element)
                )
                
                start_time = time.time()
                element.click()
                duration_ms = int((time.time() - start_time) * 1000)
                
                if self.logger:
                    self.logger.action(
                        'click_at_index',
                        element_name=element_name,
                        selector_method=method,
                        duration_ms=duration_ms,
                        success=True,
                        details=f"Clicked element at index {index} of {len(elements)}"
                    )
                
                return True
                
            except StaleElementReferenceException as e:
                last_exception = e
                if self.logger:
                    self.logger.warning(
                        f"Element {element_name}[{index}] a devenit învechit, se reîncearcă...",
                        f"Stale element at index {index}, retrying (attempt {attempt}/{retries})"
                    )
                time.sleep(RETRY_DELAY * attempt)
                
            except IndexError:
                raise  # Don't retry index errors
                
            except Exception as e:
                last_exception = e
                if attempt < retries:
                    time.sleep(RETRY_DELAY * attempt)
        
        if self.logger:
            self.logger.error(
                f"Nu s-a putut apăsa pe {element_name}[{index}]",
                f"Failed to click {element_name} at index {index} after {retries} attempts",
                error=last_exception
            )
        raise last_exception

    def wait_and_click(self, element_name, timeout=TIMEOUT_MEDIUM, retries=MAX_RETRIES):
        """Find element, verify clickable, click with retry logic."""
        last_exception = None

        for attempt in range(1, retries + 1):
            try:
                element, method = self.find(element_name, timeout=timeout)

                # Wait for element to be clickable
                selectors = self.registry.get(element_name)
                if method == "css":
                    by_type, selector = By.CSS_SELECTOR, selectors.get("css")
                elif method == "xpath":
                    by_type, selector = By.XPATH, selectors.get("xpath")
                else:
                    by_type, selector = By.XPATH, f"//*[contains(text(), '{selectors.get('text')}')]"

                clickable_element = WebDriverWait(self.driver, TIMEOUT_SHORT).until(
                    EC.element_to_be_clickable((by_type, selector))
                )

                start_time = time.time()
                clickable_element.click()
                duration_ms = int((time.time() - start_time) * 1000)

                if self.logger:
                    self.logger.action(
                        'click',
                        element_name=element_name,
                        selector_method=method,
                        duration_ms=duration_ms,
                        success=True
                    )

                return True

            except StaleElementReferenceException as e:
                last_exception = e
                if self.logger:
                    self.logger.warning(
                        f"Element {element_name} a devenit învechit, se reîncearcă...",
                        f"Stale element {element_name}, retrying (attempt {attempt}/{retries})"
                    )
                time.sleep(RETRY_DELAY * attempt)

            except Exception as e:
                last_exception = e
                if attempt < retries:
                    time.sleep(RETRY_DELAY * attempt)

        if self.logger:
            self.logger.error(
                f"Nu s-a putut apăsa pe {element_name}",
                f"Failed to click {element_name} after {retries} attempts",
                error=last_exception
            )
        raise last_exception

    def input_text(self, element_name, text, timeout=TIMEOUT_MEDIUM, clear_first=True):
        """Find input element and enter text."""
        element, method = self.find(element_name, timeout=timeout)

        if clear_first:
            element.clear()

        start_time = time.time()
        element.send_keys(text)
        duration_ms = int((time.time() - start_time) * 1000)

        if self.logger:
            self.logger.action(
                'input_text',
                element_name=element_name,
                selector_method=method,
                duration_ms=duration_ms,
                success=True,
                details=f"Entered {len(text)} chars"
            )

        return element

    def get_text(self, element_name, timeout=TIMEOUT_MEDIUM, required=True):
        """Find element and return its text content."""
        element, method = self.find(element_name, timeout=timeout, required=required)
        if element:
            return element.text.strip()
        return None

    def is_displayed(self, element_name, timeout=TIMEOUT_SHORT):
        """Check if element is displayed on page."""
        try:
            element, _ = self.find(element_name, timeout=timeout, required=False)
            return element is not None and element.is_displayed()
        except Exception:
            return False

    def capture_screenshot(self, name_prefix="error"):
        """Capture screenshot and return the file path."""
        if not os.path.exists(SCREENSHOTS_DIR):
            os.makedirs(SCREENSHOTS_DIR)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{name_prefix}_{timestamp}.png"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)

        try:
            self.driver.save_screenshot(filepath)
            if self.logger:
                self.logger.debug(f"Screenshot salvat: {filepath}", f"Screenshot saved: {filepath}")
            return filepath
        except Exception as e:
            if self.logger:
                self.logger.warning(f"Nu s-a putut salva screenshot: {e}", f"Failed to save screenshot: {e}")
            return None


# =============================================================================
# STATE VERIFIER
# =============================================================================

class StateVerifier:
    """
    Verifies that actions completed successfully.
    Captures state on errors for debugging.
    """

    def __init__(self, driver, logger, element_finder):
        self.driver = driver
        self.logger = logger
        self.finder = element_finder

    def verify_page_loaded(self, expected_elements, timeout=TIMEOUT_MEDIUM):
        """Verify page loaded with expected elements present."""
        missing = []
        for element_name in expected_elements:
            try:
                self.finder.find(element_name, timeout=timeout // len(expected_elements), required=True)
            except ElementNotFoundError:
                missing.append(element_name)

        if missing:
            if self.logger:
                self.logger.error(
                    f"Pagina nu s-a încărcat corect. Lipsesc: {missing}",
                    f"Page load failed. Missing elements: {missing}"
                )
            return False, missing
        return True, []

    def verify_subscription_valid(self, timeout=TIMEOUT_SHORT):
        """Verify subscription code was accepted (no error alerts visible)."""
        time.sleep(0.5)  # Brief wait for potential error to appear

        # Check for error alerts
        if self.finder.is_displayed("error_alert", timeout=timeout):
            error_text = self.finder.get_text("error_alert", required=False)
            if self.logger:
                self.logger.error(
                    f"Cod de abonament invalid: {error_text}",
                    f"Invalid subscription code: {error_text}"
                )
            return False, error_text

        # Also check for the specific Romanian error message
        try:
            elements = self.driver.find_elements(By.XPATH, "//*[contains(text(), 'Nu au fost gasite abonamente')]")
            for element in elements:
                if element.is_displayed():
                    error_text = element.text.strip()
                    if self.logger:
                        self.logger.error(
                            f"Abonament negăsit: {error_text}",
                            f"Subscription not found: {error_text}"
                        )
                    return False, error_text
        except Exception:
            pass

        return True, None

    def verify_reservation_count_visible(self, timeout=TIMEOUT_MEDIUM):
        """Verify that reservation count element is visible after subscription validation."""
        try:
            self.finder.find("reservation_count_span", timeout=timeout)
            return True, None
        except ElementNotFoundError as e:
            if self.logger:
                self.logger.error(
                    "Nu s-a găsit numărul de rezervări disponibile",
                    "Reservation count not found - subscription may be invalid"
                )
            return False, str(e)

    def verify_calendar_loaded(self, timeout=TIMEOUT_MEDIUM):
        """Verify calendar table is loaded and has dates."""
        try:
            self.finder.find("calendar_table", timeout=timeout)
            return True, None
        except ElementNotFoundError as e:
            if self.logger:
                self.logger.error(
                    "Calendarul nu s-a încărcat",
                    "Calendar failed to load"
                )
            return False, str(e)

    def verify_slots_loaded(self, timeout=TIMEOUT_MEDIUM):
        """Verify time slots are loaded for selected date."""
        try:
            elements, _ = self.finder.find_all("time_slot", timeout=timeout)
            if elements:
                return True, len(elements)
            return False, 0
        except Exception as e:
            if self.logger:
                self.logger.error(
                    "Nu s-au găsit intervale orare",
                    "Time slots not found"
                )
            return False, 0

    def capture_state_on_error(self, error_context="unknown"):
        """Capture full page state and screenshot on error."""
        state = {
            "context": error_context,
            "url": self.driver.current_url,
            "title": self.driver.title,
            "timestamp": datetime.now().isoformat()
        }

        # Capture screenshot
        screenshot_path = self.finder.capture_screenshot(f"error_{error_context}")
        state["screenshot"] = screenshot_path

        if self.logger:
            self.logger.debug(
                f"Stare capturată pentru eroare: {error_context}",
                f"Error state captured: {state}"
            )

        return state


# =============================================================================
# AVAILABILITY COLLECTOR
# =============================================================================

class AvailabilityCollector:
    """
    Silent data collection mode for cron jobs.
    No user interaction, just logs availability data.
    """

    def __init__(self, driver, db_manager, logger, element_finder, verifier, timing=None):
        self.driver = driver
        self.db = db_manager
        self.logger = logger
        self.finder = element_finder
        self.verifier = verifier
        self.session_id = None
        self.timing = timing or TimingConfig.COLLECT

    def collect_all_subscriptions(self, subscription_codes):
        """
        Collect availability for all configured subscription codes.
        Returns exit code.
        """
        if not subscription_codes:
            if self.logger:
                self.logger.error(
                    "Nu s-au găsit coduri de abonament",
                    "No subscription codes found"
                )
            return ExitCode.INVALID_SUBSCRIPTION

        total_collected = 0
        errors = 0

        for code_info in subscription_codes:
            code = code_info['code']
            name = code_info.get('name', 'Unknown')

            if self.logger:
                self.logger.info(
                    f"Se colectează date pentru {name} ({code})",
                    f"Collecting data for {name} ({code})"
                )

            try:
                collected = self.collect_for_subscription(code, name)
                total_collected += collected
            except Exception as e:
                errors += 1
                if self.logger:
                    self.logger.error(
                        f"Eroare la colectarea datelor pentru {name}",
                        f"Error collecting data for {name}: {e}",
                        error=e
                    )

        if self.logger:
            self.logger.info(
                f"Colectare completă: {total_collected} sloturi, {errors} erori",
                f"Collection complete: {total_collected} slots, {errors} errors"
            )

        if errors > 0 and total_collected == 0:
            return ExitCode.NETWORK_ERROR
        return ExitCode.SUCCESS

    def collect_for_subscription(self, code, name):
        """
        Collect all available dates and slots for one subscription.
        Now supports multi-month navigation.
        Returns number of slots collected.
        """
        collected_count = 0
        max_months_to_check = 2  # Check current and next month
        months_checked = 0
        seen_dates = set()  # Track processed dates to avoid duplicates

        # Navigate to the booking page
        self.driver.get("https://bpsb.registo.ro/client-interface/appointment-subscription/step1")
        time.sleep(self.timing['page_load'])

        # Enter subscription code
        try:
            self.finder.input_text("subscription_input", code)
            self.finder.wait_and_click("search_button")
            time.sleep(self.timing['subscription_input'])
        except Exception as e:
            if self.logger:
                self.logger.error(f"Nu s-a putut introduce codul", f"Failed to enter code: {e}")
            raise

        # Verify subscription is valid
        is_valid, error = self.verifier.verify_subscription_valid()
        if not is_valid:
            if self.logger:
                self.logger.warning(f"Abonament invalid: {code}", f"Invalid subscription: {code}")
            raise InvalidSubscriptionError(f"Subscription {code} is invalid: {error}")

        # Click sauna option
        try:
            self.finder.wait_and_click("sauna_option_button")
            time.sleep(self.timing['sauna_click'])
        except Exception as e:
            if self.logger:
                self.logger.error(f"Nu s-a putut selecta optiunea", f"Failed to click sauna option: {e}")
            raise

        # Get available dates from calendar (use longer timeout)
        is_loaded, _ = self.verifier.verify_calendar_loaded(timeout=TIMEOUT_LONG)
        if not is_loaded:
            raise BookingError("Calendar failed to load")

        # Process multiple months
        while months_checked < max_months_to_check:
            month_name, year = self._get_current_calendar_month()
            if self.logger:
                self.logger.debug(
                    f"Se proceseaza luna: {month_name} {year}",
                    f"Processing month: {month_name} {year}"
                )

            # Extract dates from current view
            available_dates = self._extract_available_dates()
            date_strings = [d['date'] for d in available_dates if d['date'] not in seen_dates]

            if self.logger and date_strings:
                self.logger.debug(
                    f"S-au gasit {len(date_strings)} date noi in {month_name}",
                    f"Found {len(date_strings)} new dates in {month_name}"
                )

            # Process each date
            for date_str in date_strings:
                seen_dates.add(date_str)
                collected_count += self._process_single_date(date_str, code, name)

            months_checked += 1

            # Try to navigate to next month if we have more to check
            if months_checked < max_months_to_check:
                if not self._navigate_to_next_month():
                    if self.logger:
                        self.logger.debug(
                            "Nu se poate naviga mai departe",
                            "Cannot navigate further, stopping"
                        )
                    break  # Cannot navigate further
                time.sleep(self.timing['calendar_nav'])

        return collected_countt

    def _extract_available_dates(self):
        """Extract available dates from the calendar."""
        available_dates = []

        try:
            table_element, _ = self.finder.find("calendar_table")

            # Get month/year from header
            current_month = ""
            current_year = ""

            try:
                header_text = self.finder.get_text("calendar_header", required=False)
                if header_text:
                    parts = header_text.split()
                    if len(parts) >= 2:
                        month_name = parts[0].lower()
                        current_year = parts[1]

                        month_names = {
                            'ianuarie': '01', 'februarie': '02', 'martie': '03', 'aprilie': '04',
                            'mai': '05', 'iunie': '06', 'iulie': '07', 'august': '08',
                            'septembrie': '09', 'octombrie': '10', 'noiembrie': '11', 'decembrie': '12',
                            'january': '01', 'february': '02', 'march': '03', 'april': '04',
                            'may': '05', 'june': '06', 'july': '07', 'august': '08',
                            'september': '09', 'october': '10', 'november': '11', 'december': '12'
                        }
                        current_month = month_names.get(month_name, f"{datetime.now().month:02d}")
            except Exception:
                now = datetime.now()
                current_month = f"{now.month:02d}"
                current_year = str(now.year)

            # Find date cells
            date_cells = table_element.find_elements(By.TAG_NAME, "td")

            for cell in date_cells:
                if "disabled" not in cell.get_attribute("class"):
                    date_text = cell.text.strip()
                    if date_text:
                        day = date_text.zfill(2)
                        formatted_date = f"{current_year}-{current_month}-{day}"
                        available_dates.append({
                            "date": formatted_date,
                            "element": cell
                        })

        except Exception as e:
            if self.logger:
                self.logger.warning(f"Eroare la extragerea datelor", f"Error extracting dates: {e}")

        return available_dates

    def _extract_slots_for_date(self):
        """Extract time slots and availability for the currently selected date."""
        slots = []
        
        # Regex patterns for valid time slots
        # Matches: "07:00 - 10:30", "10:30-14:00", etc.
        TIME_SLOT_PATTERN = re.compile(r'^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$')
        # Matches: "Grupa 07:00 - 10:30" format
        GRUPA_TIME_PATTERN = re.compile(r'Grupa\s+(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})')
        
        # Known error message patterns to skip
        ERROR_PATTERNS = [
            "nu au fost gasite",
            "nu exista", 
            "indisponibil",
            "nicio disponibilitate",
            "nu sunt disponibile"
        ]

        try:
            slot_elements, _ = self.finder.find_all("time_slot")

            for slot_element in slot_elements:
                slot_text = slot_element.text.strip()
                
                # Skip empty text
                if not slot_text:
                    continue
                
                # Check if any error pattern matches (case-insensitive)
                is_error = any(pattern in slot_text.lower() for pattern in ERROR_PATTERNS)
                if is_error:
                    if self.logger:
                        self.logger.debug(
                            f"Se sare mesajul de eroare: {slot_text[:50]}...",
                            f"Skipping error message: {slot_text[:50]}..."
                        )
                    continue

                time_str = None
                available_places = 0

                # Parse each line looking for time and availability
                for line in slot_text.split('\n'):
                    line = line.strip()
                    
                    # Check for availability count
                    if "Locuri disponibile:" in line:
                        try:
                            available_places = int(line.split(':')[1].strip())
                        except (ValueError, IndexError):
                            pass
                        continue
                    
                    # Check for direct time format "07:00 - 10:30"
                    if TIME_SLOT_PATTERN.match(line):
                        time_str = line
                        continue
                    
                    # Check for "Grupa 07:00 - 10:30" format
                    grupa_match = GRUPA_TIME_PATTERN.search(line)
                    if grupa_match:
                        time_str = grupa_match.group(1)
                        continue

                # Only add if we found a valid time string
                if time_str:
                    slots.append({
                        "time": time_str,
                        "available": available_places,
                        "text": slot_text
                    })
                else:
                    # Log warning for unrecognized slot format (for debugging)
                    if self.logger and slot_text:
                        self.logger.debug(
                            f"Format slot nerecunoscut: {slot_text[:50]}",
                            f"Unrecognized slot format: {slot_text[:50]}"
                        )

        except Exception as e:
            # No slots found is not an error - it's just an empty day
            if self.logger:
                self.logger.debug(
                    f"Niciun slot găsit sau eroare",
                    f"No slots found or error: {e}"
                )

        return slots

    def _navigate_to_next_month(self):
        """Navigate to the next month in the calendar. Returns True if successful."""
        try:
            self.finder.wait_and_click("next_month_arrow")
            time.sleep(self.timing['next_month'])
            return True
        except Exception as e:
            if self.logger:
                self.logger.debug(
                    "Nu s-a putut naviga la luna urmatoare",
                    f"Could not navigate to next month: {e}"
                )
            return False

    def _get_current_calendar_month(self):
        """Get the current month/year displayed in calendar header."""
        try:
            header_text = self.finder.get_text("calendar_header", required=False)
            if header_text:
                parts = header_text.split()
                if len(parts) >= 2:
                    return parts[0].lower(), parts[1]  # month_name, year
        except Exception:
            pass
        return None, None

    def _process_single_date(self, date_str, code, name):
        """Process a single date and collect slot data. Returns slots collected count."""
        try:
            # Re-fetch the calendar and find the date element fresh
            fresh_dates = self._extract_available_dates()
            date_element = None
            for d in fresh_dates:
                if d['date'] == date_str:
                    date_element = d['element']
                    break

            if not date_element:
                if self.logger:
                    self.logger.debug(
                        f"Nu s-a gasit data {date_str}",
                        f"Date {date_str} not found in calendar"
                    )
                return 0

            # Click on the date to see slots
            date_element.click()
            time.sleep(self.timing['date_click'])

            # Get slots for this date
            slots = self._extract_slots_for_date()

            slots_logged = 0
            for slot in slots:
                self.db.log_availability(
                    self.session_id,
                    code,
                    date_str,
                    slot['time'],
                    slot['available'],
                    subscription_name=name
                )
                slots_logged += 1

            if self.logger and slots:
                self.logger.debug(
                    f"Colectat {len(slots)} sloturi pentru {date_str}",
                    f"Collected {len(slots)} slots for {date_str}"
                )

            # Navigate back to calendar to process next date
            self.driver.back()
            time.sleep(self.timing['back_reload'])

            return slots_logged

        except StaleElementReferenceException:
            if self.logger:
                self.logger.debug(
                    f"Element invechit pentru {date_str}, se sare",
                    f"Stale element for {date_str}, skipping"
                )
            return 0

        except Exception as e:
            if self.logger:
                self.logger.debug(
                    f"Nu s-au putut colecta sloturile pentru {date_str}",
                    f"Failed to collect slots for {date_str}: {e}"
                )
            return 0

    def set_session(self, session_id):
        """Set the session ID for logging."""
        self.session_id = session_id


# =============================================================================
# HELPER FUNCTIONS (ORIGINAL - kept for backward compatibility)
# =============================================================================

def get_day_name_ro(date_str):
    """
    Get Romanian day name from a date string in DD-MM-YYYY format.
    Returns the day name in Romanian (e.g., "Luni", "Marți", etc.)
    """
    day_names_ro = {
        0: "Luni",
        1: "Marți",
        2: "Miercuri",
        3: "Joi",
        4: "Vineri",
        5: "Sâmbătă",
        6: "Duminică"
    }
    try:
        # Parse DD-MM-YYYY format
        date_obj = datetime.strptime(date_str, "%d-%m-%Y")
        return day_names_ro[date_obj.weekday()]
    except (ValueError, KeyError):
        return ""


def parse_slot_info(slot_element):
    """
    Extracts detailed information from a time slot element.
    Returns a dictionary with slot details including available places.
    """
    try:
        slot_text = slot_element.text.strip()
        # Extract the number of available places
        # Looking for pattern "Locuri disponibile: X"
        print("_________________________________________________________")
        print("Parsing slot info:")
        available_places = 0
        for line in slot_text.split('\n'):
            print(line)
            if "Locuri disponibile:" in line:
                available_places = int(line.split(':')[1].strip())
                print(f"Found {available_places} available places")
                break

        return {
            "text": slot_text,
            "element": slot_element,
            "available_places": available_places
        }
    except Exception as e:
        print(f"Error parsing slot info: {str(e)}")
        return None

def get_available_timeslots(driver):
    """
    Finds and returns information about available time slots with their capacity.
    """
    try:
        # Wait for time slots to be present
        time.sleep(0.5)
        slots = driver.find_elements(By.CLASS_NAME, "alert-outline-primary")

        available_slots = []
        for i, slot in enumerate(slots, 1):
            slot_info = parse_slot_info(slot)
            if slot_info:
                slot_info["number"] = i
                available_slots.append(slot_info)

        return available_slots
    except Exception as e:
        print(f"Error getting time slots: {str(e)}")
        return []

def validate_slot_selections(selected_slots, requested_quantity, remaining_reservations):
    """
    Validates if the selected slots meet all constraints.
    Returns tuple (is_valid, message)
    """
    total_places = sum(slot["available_places"] for slot in selected_slots)

    if len(selected_slots) != requested_quantity:
        return False, f"Trebuie să selectați exact {requested_quantity} sloturi."

    if requested_quantity > remaining_reservations:
        return False, f"Ați solicitat {requested_quantity} sloturi dar aveți doar {remaining_reservations} rezervări rămase."

    # Check each slot's availability
    for slot in selected_slots:
        if requested_quantity > slot["available_places"]:
            return False, f"Slotul {slot['number']} are doar {slot['available_places']} locuri disponibile, dar ați solicitat {requested_quantity}."

    return True, "Selecția este validă"

def select_multiple_slots(available_slots, quantity):
    """
    Allows user to select multiple slots at once.
    Returns list of selected slots.
    """
    selected_slots = []
    print("\nVă rugăm introduceți numerele tuturor sloturilor pe care doriți să le selectați, separate prin spații.")
    print(f"Trebuie să selectați {quantity} sloturi.")

    while True:
        try:
            selections = input("Introduceți numerele sloturilor: ").strip().split()

            # Convert to integers and validate
            slot_numbers = [int(x) for x in selections]

            # Validate quantity
            if len(slot_numbers) != quantity:
                print(f"Vă rugăm selectați exact {quantity} sloturi.")
                continue

            # Validate range
            if not all(1 <= num <= len(available_slots) for num in slot_numbers):
                print(f"Vă rugăm introduceți numere între 1 și {len(available_slots)}")
                continue

            # Get the selected slots
            selected_slots = [available_slots[num-1] for num in slot_numbers]
            return selected_slots

        except ValueError:
            print("Vă rugăm introduceți numere valide separate prin spații.")

def get_remaining_reservations(driver):
    """
    Extracts the number of remaining reservations from the h5 element.
    Returns the number of remaining reservations allowed.
    """
    try:
        h5_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/h5"))
        )
        # Extract text and split by "/"
        text = h5_element.text
        remaining = int(text.split("/")[1].strip())
        return remaining
    except Exception as e:
        print(f"Error getting remaining reservations: {str(e)}")
        return 0


def validate_quantity(requested_quantity, remaining_reservations, available_slots):
    """
    Validates if the requested quantity is possible given the constraints.
    Returns tuple (is_valid, message)
    """
    if requested_quantity > remaining_reservations:
        return False, f"Ați solicitat {requested_quantity} sloturi dar aveți doar {remaining_reservations} rezervări rămase."

    if requested_quantity > len(available_slots):
        return False, f"Ați solicitat {requested_quantity} sloturi dar sunt disponibile doar {len(available_slots)} intervale orare."

    return True, "Cantitatea este validă"

def count_available_dates(driver, table_xpath):
    """
    Counts how many available dates are present in the current calendar view.
    Returns the count of available dates.
    """
    try:
        # Wait for the table to be present
        table = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, table_xpath))
        )

        # Find all date cells
        date_cells = table.find_elements(By.TAG_NAME, "td")

        # Count cells that are not disabled
        available_count = sum(
            1 for cell in date_cells
            if "disabled" not in cell.get_attribute("class") and cell.text.strip()
        )

        return available_count
    except Exception as e:
        print(f"Error counting available dates: {str(e)}")
        return 0


def check_and_navigate_calendar(driver, table_xpath, next_month_arrow_xpath, minimum_days=15):
    """
    Checks if the current month has enough available dates.
    If not, clicks to the next month.
    Returns True if navigation was needed, False otherwise.
    """
    available_count = count_available_dates(driver, table_xpath)
    print(f"S-au găsit {available_count} date disponibile în vizualizarea curentă")

    if available_count < minimum_days:
        try:
            # Find and click the next month arrow
            next_month_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, next_month_arrow_xpath))
            )
            next_month_button.click()
            print("S-a navigat la luna următoare din cauza datelor disponibile insuficiente")

            # Add a small delay to let the calendar update
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"Error navigating to next month: {str(e)}")
            return False
    return False


def get_future_dates(days=30):
    """
    Generate a list of dates from today until specified number of days in the future.
    Returns a list of dates in the format needed for the website.
    """
    dates = []
    today = datetime.now()
    for i in range(days + 1):
        future_date = today + timedelta(days=i)
        dates.append(future_date.strftime("%Y-%m-%d"))
    return dates


def check_for_subscription_error(driver):
    """
    Checks for error messages after entering subscription code.
    Returns tuple (has_error, error_message)
    """
    try:
        # Wait a short time for any error alerts to appear
        time.sleep(0.5)

        # Look for common error alert patterns
        error_selectors = [
            ".alert-danger",
            ".alert-error",
            "[class*='alert'][class*='danger']",
            "[class*='alert'][class*='error']",
            ".error-message"
        ]

        for selector in error_selectors:
            try:
                error_elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in error_elements:
                    if element.is_displayed() and element.text.strip():
                        error_text = element.text.strip()
                        print(f"Error detected: {error_text}")
                        return True, error_text
            except:
                continue

        # Check for specific Romanian error message
        try:
            elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Nu au fost gasite abonamente')]")
            for element in elements:
                if element.is_displayed():
                    error_text = element.text.strip()
                    print(f"Subscription error detected: {error_text}")
                    return True, error_text
        except:
            pass

        return False, ""

    except Exception as e:
        print(f"Error while checking for subscription errors: {str(e)}")
        return False, ""


def choose_subscription_code():
    """
    Load subscription codes from environment variable and help user choose.
    """
    codes = load_subscription_codes()
    
    if not codes:
        print("\n[X] Nu s-au gasit coduri de abonament!")
        print("Configurati variabila NEPTUN_SUBSCRIPTIONS in fisierul .env")
        print("Format: NEPTUN_SUBSCRIPTIONS='cod1:nume1,cod2:nume2'")
        print("Exemplu: NEPTUN_SUBSCRIPTIONS='5642ece785:Kicky,3adc06c0e8:Adrian'")
        sys.exit(ExitCode.INVALID_SUBSCRIPTION)
    
    # Display available codes
    print("\nCoduri de abonament disponibile:")
    for i, code_info in enumerate(codes, 1):
        print(f"{i}. {code_info['code']} {code_info['name']}")
    
    # Get user selection
    while True:
        try:
            choice = input(f"\nVa rugam selectati un cod (1-{len(codes)}): ")
            choice_num = int(choice)
            
            if 1 <= choice_num <= len(codes):
                selected_code = codes[choice_num - 1]['code']
                print(f"Cod selectat: {selected_code} ({codes[choice_num - 1]['name']})")
                return selected_code
            else:
                print(f"Alegere invalida. Va rugam selectati intre 1 si {len(codes)}.")
                
        except ValueError:
            print("Va rugam introduceti un numar valid.")


def choose_subscription_code_fallback():
    """
    Fallback function with hardcoded subscription codes when CSV is not available.
    """
    print("\\nCoduri de abonament disponibile (implicit):")
    print("1. 5642ece785 Kicky")
    print("2. 3adc06c0e8 Adrian")

    while True:
        choice = input("\\nVă rugăm selectați un cod (1 sau 2): ")
        if choice == "1":
            return "5642ece785"
        elif choice == "2":
            return "3adc06c0e8"
        else:
            print("Alegere invalidă. Vă rugăm selectați 1 sau 2.")


def get_max_reservations(driver):
    """
    Extracts the maximum number of available reservations from the span element.
    This function is called right after entering the subscription code.
    Returns tuple (success, max_reservations) where success indicates if operation was successful.
    """
    try:
        # Wait for the span element containing the reservation info to be present
        # Use shorter timeout to fail faster if element doesn't exist
        reservation_span = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/button/span[2]"))
        )

        # Check if element is visible and contains text
        if not reservation_span.is_displayed():
            print("Reservation element found but not visible")
            return False, 0

        # Extract text and split by ":" to get the number
        text = reservation_span.text.strip()
        if not text or ":" not in text:
            print(f"Unexpected text format in reservation element: '{text}'")
            return False, 0

        try:
            max_reservations = int(text.split(":")[1].strip())
            print(f"Found maximum reservations: {max_reservations}")
            return True, max_reservations
        except (ValueError, IndexError) as e:
            print(f"Could not parse reservation number from text '{text}': {str(e)}")
            return False, 0

    except TimeoutException:
        print("Reservation information element not found - this may indicate an invalid subscription")
        return False, 0
    except Exception as e:
        print(f"Unexpected error getting maximum reservations: {str(e)}")
        return False, 0

def get_quantity(max_reservations):
    """
    Get the desired quantity of items from user, ensuring it doesn't exceed the maximum.
    """
    while True:
        try:
            print(f"\nPuteți rezerva până la {max_reservations} rezervări.")
            quantity = int(input("Câte rezervări doriți să faceți? "))
            if 0 < quantity <= max_reservations:
                return quantity
            elif quantity <= 0:
                print("Vă rugăm introduceți un număr mai mare de 0.")
            else:
                print(f"Nu puteți rezerva mai mult de {max_reservations} rezervări.")
        except ValueError:
            print("Vă rugăm introduceți un număr valid.")

def get_available_dates(driver, table_xpath):
    """
    Extract available dates from the calendar table.
    Returns a list of available dates and their corresponding elements.
    """
    available_dates = []

    # Wait for the table to be present
    table = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, table_xpath))
    )

    # Extract month and year from calendar header
    current_month = ""
    current_year = ""
    
    try:
        # Try to find the month/year display in the calendar header
        # Look forthe heade r that contains the month/year text
        header_xpath = table_xpath.replace("/tbody", "/thead/tr[1]/th[2]")
        month_year_element = driver.find_element(By.XPATH, header_xpath)
        header_text = month_year_element.text.strip()
        
        # Parse month and year from header text (format might be "September 2025" or similar)
        if header_text:
            parts = header_text.split()
            if len(parts) >= 2:
                month_name = parts[0]
                current_year = parts[1]
                
                # Convert month name to number
                month_names = {
                    'ianuarie': '01', 'februarie': '02', 'martie': '03', 'aprilie': '04',
                    'mai': '05', 'iunie': '06', 'iulie': '07', 'august': '08',
                    'septembrie': '09', 'octombrie': '10', 'noiembrie': '11', 'decembrie': '12',
                    'january': '01', 'february': '02', 'march': '03', 'april': '04',
                    'may': '05', 'june': '06', 'july': '07', 'august': '08',
                    'september': '09', 'october': '10', 'november': '11', 'december': '12'
                }
                current_month = month_names.get(month_name.lower(), "09")  # Default to September
    except Exception as e:
        # If we can't extract month/year, use current date as fallback
        from datetime import datetime
        now = datetime.now()
        current_month = f"{now.month:02d}"
        current_year = str(now.year)
        print(f"Nu s-a putut extrage antetul calendarului, se folosește luna/anul curent (cu detecție automată a lunii următoare):")

    # Find all date cells
    date_cells = table.find_elements(By.TAG_NAME, "td")

    # Track previous day to detect month wrap-around
    prev_day = 0

    for cell in date_cells:
        # Check if the cell is not disabled
        if "disabled" not in cell.get_attribute("class"):
            date_text = cell.text
            if date_text.strip():  # Ensure the cell contains a date
                day_num = int(date_text.strip())

                # Detect month wrap-around (e.g., 31 -> 3 means we crossed into next month)
                if prev_day > 20 and day_num < 10:
                    # Increment month
                    month_int = int(current_month)
                    if month_int == 12:
                        current_month = "01"
                        current_year = str(int(current_year) + 1)
                    else:
                        current_month = f"{month_int + 1:02d}"

                prev_day = day_num

                # Format as DD-MM-YYYY
                day = str(day_num).zfill(2)  # Pad single digits with leading zero
                formatted_date = f"{day}-{current_month}-{current_year}"

                available_dates.append({
                    "date": formatted_date,
                    "element": cell
                })

    return available_dates


def process_slot_selection(driver, finder, slot, is_last_slot=False):
    """
    Processes a single slot selection including all necessary button clicks.
    Uses indexed selection to click the correct Selecteaza button.

    Parameters:
    - driver: Selenium WebDriver instance
    - finder: ElementFinder instance for robust element location
    - slot: The slot information dictionary (must include 'number' and 'text')
    - is_last_slot: Boolean indicating if this is the last slot being processed
    """
    try:
        slot_number = slot['number']
        
        # Click the slot container first
        print(f"\nSe selectează slotul {slot_number}: {slot['text']}")
        slot['element'].click()
        time.sleep(0.5)  # Wait for the selection to register

        # Click the Nth Selecteaza button using indexed selection
        # This ensures we click the button for the slot the user selected
        print(f"Se apasă butonul 'Selectează' pentru slotul {slot_number}...")
        finder.click_at_index("slot_select_buttons", slot_number)
        print(f"S-a apăsat butonul 'Selectează' pentru slotul {slot_number}")
        time.sleep(0.5)  # Wait for cart to appear

        # If this is not the last slot, we need to exit the cart
        if not is_last_slot:
            finder.wait_and_click("cart_close_button")
            print("S-a închis coșul pentru a continua selecția")
            time.sleep(0.5)  # Wait for cart to close
        else:
            # This is the last slot, so click the final confirmation button
            print("Se procesează selecția finală...")
            finder.wait_and_click("final_confirm_button")
            print("Selecția finală completată!")

    except TimeoutException as e:
        print(f"Timeout la procesarea slotului {slot.get('number', '?')}: {str(e)}")
        raise
    except IndexError as e:
        print(f"Eroare de index pentru slotul {slot.get('number', '?')}: {str(e)}")
        raise
    except Exception as e:
        print(f"Eroare la procesarea selecției slotului: {str(e)}")
        raise


# =============================================================================
# POST-BOOKING VERIFICATION
# =============================================================================

def is_login_page(driver):
    """
    Check if the current page is a login page.
    
    Returns:
        True if on login page, False otherwise
    """
    try:
        current_url = driver.current_url.lower()
        if 'login' in current_url or 'auth' in current_url:
            return True
        
        # Also check for login form elements
        login_indicators = [
            "//input[@type='password']",
            "//form[contains(@action, 'login')]",
            "//*[contains(text(), 'Autentificare') or contains(text(), 'Login')]"
        ]
        
        for xpath in login_indicators:
            try:
                elements = driver.find_elements(By.XPATH, xpath)
                if elements:
                    return True
            except:
                continue
        
        return False
    except:
        return False


def perform_login(driver, finder, credentials):
    """
    Perform login on the BPSB website.
    
    Parameters:
    - driver: Selenium WebDriver instance
    - finder: ElementFinder instance
    - credentials: dict with 'email' and 'password' keys
    
    Returns:
        True if login successful, False otherwise
    """
    try:
        print("\nSe efectuează autentificarea...")
        
        if not credentials or not credentials.get('email') or not credentials.get('password'):
            print("❌ Credențiale lipsă. Verificați fișierul .env")
            return False
        
        # Enter email
        finder.input_text("login_email_input", credentials['email'])
        print("✓ Email introdus")
        
        # Enter password
        finder.input_text("login_password_input", credentials['password'])
        print("✓ Parolă introdusă")
        
        # Click login button
        finder.wait_and_click("login_submit_button")
        print("✓ Buton login apăsat")
        
        # Wait for page to load after login
        time.sleep(2)
        
        # Check if login was successful (no longer on login page)
        if is_login_page(driver):
            print("❌ Autentificare eșuată - încă pe pagina de login")
            return False
        
        print("✓ Autentificare reușită!")
        return True
        
    except Exception as e:
        print(f"❌ Eroare la autentificare: {str(e)}")
        return False


def verify_booking(driver, finder, expected_date, expected_time_slot, credentials=None):
    """
    Navigate to appointments page and verify that the booking exists.
    
    Parameters:
    - driver: Selenium WebDriver instance
    - finder: ElementFinder instance
    - expected_date: The date that was booked (string, e.g., "21.01.2026")
    - expected_time_slot: The time slot that was booked (string, e.g., "10:30 - 14:00")
    - credentials: Optional dict with 'email' and 'password' for login
    
    Returns:
        tuple: (success: bool, details: dict)
    """
    appointments_url = "https://bpsb.registo.ro/client-user/appointments"
    
    try:
        print(f"\n{'='*50}")
        print("VERIFICARE REZERVARE")
        print(f"{'='*50}")
        print(f"Se verifică rezervarea pentru:")
        print(f"  Data: {expected_date}")
        print(f"  Interval: {expected_time_slot}")
        
        # Navigate to appointments page
        print(f"\nSe navighează la {appointments_url}...")
        driver.get(appointments_url)
        time.sleep(1)
        
        # Handle login if required
        if is_login_page(driver):
            print("Pagina necesită autentificare...")
            
            if credentials is None:
                credentials = get_credentials()
            
            if not has_credentials():
                return False, {
                    "verified": False,
                    "error": "Credențiale de autentificare lipsă. Adăugați NEPTUN_EMAIL și NEPTUN_PASSWORD în fișierul .env"
                }
            
            login_success = perform_login(driver, finder, credentials)
            if not login_success:
                return False, {
                    "verified": False,
                    "error": "Autentificare eșuată"
                }
            
            # Navigate again after login (in case of redirect)
            driver.get(appointments_url)
            time.sleep(1)
        
        # Search for the booking in the appointments list
        print("\nSe caută rezervarea în listă...")
        
        try:
            # Wait for appointments to load
            WebDriverWait(driver, TIMEOUT_MEDIUM).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(1)
            
            # Get the page source to search for our booking
            page_text = driver.find_element(By.TAG_NAME, "body").text

            # Normalize date format (convert dashes to dots: 21-01-2026 -> 21.01.2026)
            normalized_date = expected_date.replace('-', '.')

            # Check if both date and time slot are present
            # Try both original and normalized date formats
            date_found = expected_date in page_text or normalized_date in page_text
            time_found = expected_time_slot in page_text

            # Update expected_date for display if normalized version was found
            if normalized_date in page_text and expected_date not in page_text:
                expected_date = normalized_date
            
            if date_found and time_found:
                print(f"\n✓ VERIFICARE REUȘITĂ!")
                print(f"  Rezervarea pentru {expected_date} ({expected_time_slot}) a fost găsită.")
                return True, {
                    "verified": True,
                    "date": expected_date,
                    "time_slot": expected_time_slot,
                    "message": "Rezervare confirmată în sistem"
                }
            elif date_found:
                print(f"\n⚠ VERIFICARE PARȚIALĂ")
                print(f"  Data {expected_date} găsită, dar intervalul {expected_time_slot} nu a fost găsit.")
                return False, {
                    "verified": False,
                    "date_found": True,
                    "time_found": False,
                    "error": f"Data găsită, interval orar negăsit: {expected_time_slot}"
                }
            else:
                print(f"\n❌ VERIFICARE EȘUATĂ")
                print(f"  Rezervarea pentru {expected_date} nu a fost găsită în lista de programări.")
                return False, {
                    "verified": False,
                    "date_found": False,
                    "time_found": False,
                    "error": "Rezervarea nu a fost găsită în sistem"
                }
                
        except TimeoutException:
            return False, {
                "verified": False,
                "error": "Timeout la încărcarea paginii de programări"
            }
            
    except Exception as e:
        print(f"\n❌ Eroare la verificare: {str(e)}")
        return False, {
            "verified": False,
            "error": f"Eroare: {str(e)}"
        }


# =============================================================================
# APPOINTMENT MANAGEMENT (STATUS & DELETE)
# =============================================================================

def get_current_appointments(driver, finder, credentials=None):
    """
    Navigate to appointments page and retrieve all current bookings.

    Returns:
        list of dicts with appointment info: [
            {
                'index': 1,
                'resource': 'Sauna',
                'datetime': '21.01.2026 10:30 - 14:00',
                'date': '21.01.2026',
                'time': '10:30 - 14:00',
                'places': '1',
                'price': '0.00',
                'delete_id': 'RHcxWE04KzI',
                'element': <row element>
            },
            ...
        ]
    """
    appointments_url = "https://bpsb.registo.ro/client-user/appointments"
    appointments = []

    try:
        # Navigate to appointments page
        driver.get(appointments_url)
        time.sleep(1)

        # Handle login if required
        if is_login_page(driver):
            print("Pagina necesită autentificare...")

            if credentials is None:
                credentials = get_credentials()

            if not has_credentials():
                print("❌ Credențiale lipsă. Adăugați NEPTUN_EMAIL și NEPTUN_PASSWORD în .env")
                return []

            login_success = perform_login(driver, finder, credentials)
            if not login_success:
                print("❌ Autentificare eșuată")
                return []

            # Navigate again after login
            driver.get(appointments_url)
            time.sleep(1)

        # Wait for table to load
        WebDriverWait(driver, TIMEOUT_MEDIUM).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.table tbody"))
        )
        time.sleep(0.5)

        # Find all appointment rows
        rows = driver.find_elements(By.CSS_SELECTOR, "table.table tbody tr")

        for idx, row in enumerate(rows, 1):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 5:
                    # Extract appointment data
                    resource = cells[1].text.strip()
                    datetime_text = cells[2].text.strip()
                    places = cells[3].text.strip()
                    price = cells[4].text.strip()

                    # Parse date and time from datetime string (e.g., "21.01.2026 10:30 - 14:00")
                    date_part = ""
                    time_part = ""
                    if datetime_text:
                        parts = datetime_text.split(' ', 1)
                        if len(parts) >= 1:
                            date_part = parts[0]
                        if len(parts) >= 2:
                            time_part = parts[1]

                    # Get delete button data-id
                    delete_id = ""
                    try:
                        delete_btn = row.find_element(By.CSS_SELECTOR, "button.deleteAppButton")
                        delete_id = delete_btn.get_attribute("data-id")
                    except:
                        pass

                    appointments.append({
                        'index': idx,
                        'resource': resource,
                        'datetime': datetime_text,
                        'date': date_part,
                        'time': time_part,
                        'places': places,
                        'price': price,
                        'delete_id': delete_id,
                        'element': row
                    })
            except Exception as e:
                continue

        return appointments

    except Exception as e:
        print(f"❌ Eroare la încărcarea programărilor: {str(e)}")
        return []


def display_appointments(appointments):
    """Display appointments in a formatted table."""
    if not appointments:
        print("\n📭 Nu există programări viitoare.")
        return

    print(f"\n{'='*70}")
    print("PROGRAMĂRI VIITOARE")
    print(f"{'='*70}")
    print(f"{'Nr.':<5} {'Resursă':<12} {'Data':<12} {'Interval':<18} {'Locuri':<8} {'Preț':<8}")
    print(f"{'-'*70}")

    for apt in appointments:
        print(f"{apt['index']:<5} {apt['resource']:<12} {apt['date']:<12} {apt['time']:<18} {apt['places']:<8} {apt['price']:<8}")

    print(f"{'='*70}")
    print(f"Total programări: {len(appointments)}")


def delete_appointment(driver, finder, appointment, confirm=True):
    """
    Delete a specific appointment.

    Parameters:
    - driver: Selenium WebDriver instance
    - finder: ElementFinder instance
    - appointment: dict with appointment info (must include 'delete_id' or 'element')
    - confirm: If True, wait for and click confirmation dialog

    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        delete_id = appointment.get('delete_id')

        if not delete_id:
            return False, "ID-ul de ștergere nu este disponibil"

        # Find and click the delete button for this appointment
        delete_btn = driver.find_element(
            By.CSS_SELECTOR,
            f"button.deleteAppButton[data-id='{delete_id}']"
        )

        # Scroll to button and click
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", delete_btn)
        time.sleep(0.2)
        delete_btn.click()

        print(f"Se șterge programarea din {appointment['date']} {appointment['time']}...")
        time.sleep(0.5)

        if confirm:
            # Wait for confirmation dialog (usually SweetAlert2)
            try:
                confirm_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, ".swal2-confirm, button.swal2-confirm"))
                )
                confirm_btn.click()
                time.sleep(0.5)
                print(f"✓ Programarea a fost ștearsă cu succes!")
                return True, "Programare ștearsă cu succes"
            except TimeoutException:
                # Maybe no confirmation needed, check if already deleted
                return True, "Programare ștearsă (fără confirmare)"

        return True, "Buton de ștergere apăsat"

    except Exception as e:
        return False, f"Eroare la ștergere: {str(e)}"


def run_status_mode(headless=False):
    """
    Run in status mode - display current appointments.
    """
    print("\n" + "="*50)
    print("VERIFICARE STATUS PROGRAMĂRI")
    print("="*50)

    # Initialize browser
    if headless:
        browser_options = create_browser_options()
        driver = webdriver.Chrome(options=browser_options)
        print("🔇 Mod headless")
    else:
        driver = webdriver.Chrome()
        print("🪟 Mod cu fereastră")

    finder = ElementFinder(driver)

    try:
        # Get appointments
        appointments = get_current_appointments(driver, finder)

        # Display them
        display_appointments(appointments)

        return ExitCode.SUCCESS

    except Exception as e:
        print(f"❌ Eroare: {str(e)}")
        return ExitCode.UNKNOWN_ERROR
    finally:
        driver.quit()


def run_delete_mode(headless=False):
    """
    Run in delete mode - interactively delete appointments.
    """
    print("\n" + "="*50)
    print("ȘTERGERE PROGRAMĂRI")
    print("="*50)

    # Initialize browser
    if headless:
        browser_options = create_browser_options()
        driver = webdriver.Chrome(options=browser_options)
        print("🔇 Mod headless")
    else:
        driver = webdriver.Chrome()
        print("🪟 Mod cu fereastră")

    finder = ElementFinder(driver)

    try:
        # Get appointments
        appointments = get_current_appointments(driver, finder)

        if not appointments:
            print("\n📭 Nu există programări de șters.")
            return ExitCode.SUCCESS

        # Display them
        display_appointments(appointments)

        # Ask which to delete
        print("\nOpțiuni:")
        print("  - Introduceți numerele programărilor de șters (separate prin spații)")
        print("  - Introduceți 'all' pentru a șterge toate")
        print("  - Introduceți 'q' pentru a anula")

        choice = input("\nCe programări doriți să ștergeți? ").strip().lower()

        if choice == 'q' or choice == '':
            print("Operațiune anulată.")
            return ExitCode.SUCCESS

        if choice == 'all':
            to_delete = appointments
        else:
            try:
                indices = [int(x) for x in choice.split()]
                to_delete = [apt for apt in appointments if apt['index'] in indices]
            except ValueError:
                print("❌ Selecție invalidă.")
                return ExitCode.UNKNOWN_ERROR

        if not to_delete:
            print("❌ Nicio programare selectată.")
            return ExitCode.SUCCESS

        # Confirm deletion
        print(f"\nSe vor șterge {len(to_delete)} programare(ări):")
        for apt in to_delete:
            print(f"  - {apt['date']} {apt['time']} ({apt['resource']})")

        confirm = input("\nSigur doriți să continuați? (da/nu): ").strip().lower()
        if confirm not in ['da', 'yes', 'y']:
            print("Operațiune anulată.")
            return ExitCode.SUCCESS

        # Delete each appointment
        deleted_count = 0
        for apt in to_delete:
            # Refresh page to avoid stale elements
            if deleted_count > 0:
                driver.get("https://bpsb.registo.ro/client-user/appointments")
                time.sleep(1)

            success, message = delete_appointment(driver, finder, apt)
            if success:
                deleted_count += 1
            else:
                print(f"⚠ Nu s-a putut șterge: {message}")

        print(f"\n✓ {deleted_count}/{len(to_delete)} programări șterse.")
        return ExitCode.SUCCESS

    except Exception as e:
        print(f"❌ Eroare: {str(e)}")
        return ExitCode.UNKNOWN_ERROR
    finally:
        driver.quit()


def run_trends_mode(days=30, db_path=DB_FILE):
    """
    Display availability trends and analytics.
    """
    print("\n" + "="*60)
    print("ANALIZĂ DISPONIBILITATE SAUNA")
    print("="*60)

    db = DatabaseManager(db_path)

    try:
        # Collection stats
        stats = db.get_collection_stats()
        if not stats or stats[2] == 0:
            print("\n❌ Nu există date colectate. Rulați mai întâi:")
            print("   make collect")
            return ExitCode.NO_AVAILABILITY

        print(f"\n📊 Statistici Colectare:")
        print(f"   Date unice: {stats[0]}")
        print(f"   Abonamente: {stats[1]}")
        print(f"   Total înregistrări: {stats[2]}")
        print(f"   Prima colectare: {stats[3]}")
        print(f"   Ultima colectare: {stats[4]}")
        print(f"   Zile de colectare: {stats[5]}")

        # Slot popularity
        print(f"\n🕐 Popularitate pe Intervale Orare (ultimele {days} zile):")
        print(f"{'Interval':<20} {'Obs.':<8} {'Med.':<8} {'Min':<6} {'Max':<6} {'Full':<6}")
        print("-" * 60)

        slot_stats = db.get_slot_popularity(days)
        if slot_stats:
            for row in slot_stats:
                time_slot, obs, avg, min_val, max_val, fully_booked = row
                print(f"{time_slot:<20} {obs:<8} {avg:<8} {min_val:<6} {max_val:<6} {fully_booked:<6}")
        else:
            print("   Nu există date pentru perioada selectată.")

        # Day of week trends
        print(f"\n📅 Tendințe pe Zile ale Săptămânii:")
        print(f"{'Zi':<12} {'Obs.':<8} {'Med. Disponibile':<18}")
        print("-" * 40)

        dow_stats = db.get_day_of_week_trends(days)
        if dow_stats:
            for row in dow_stats:
                day_name, _, avg_avail, obs = row
                bar = "█" * int(avg_avail) if avg_avail else ""
                print(f"{day_name:<12} {obs:<8} {avg_avail:<6} {bar}")
        else:
            print("   Nu există date pentru perioada selectată.")

        # Heatmap-style output
        print(f"\n🔥 Hartă Cerere (roșu = popular, verde = disponibil):")
        slots_headers = ["07:00-10:30", "10:30-14:00", "14:00-17:30", "17:30-21:00"]
        print(f"{'Zi':<5}", end="")
        for s in slots_headers:
            print(f"{s:<14}", end="")
        print()
        print("-" * 60)

        hourly_data = db.get_hourly_demand(days)
        # Organize by day
        by_day = {}
        for row in hourly_data:
            day, slot, avg, full = row
            if day not in by_day:
                by_day[day] = {}
            by_day[day][slot] = (avg, full)

        for day in ['Lun', 'Mar', 'Mie', 'Joi', 'Vin', 'Sam', 'Dum']:
            if day in by_day:
                print(f"{day:<5}", end="")
                for slot_header in slots_headers:
                    # Normalize slot format for matching (with spaces around dash)
                    slot_key = slot_header.replace("-", " - ")
                    if slot_key in by_day[day]:
                        avg, _ = by_day[day][slot_key]
                        # Color indicator based on availability
                        if avg >= 4:
                            indicator = "🟢"
                        elif avg >= 2:
                            indicator = "🟡"
                        else:
                            indicator = "🔴"
                        print(f"{indicator} {avg:>5}     ", end="")
                    else:
                        print(f"{'--':>13} ", end="")
                print()

        print("\n💡 Legendă: 🟢 Disponibil (4+) | 🟡 Moderat (2-3) | 🔴 Popular (<2)")
        print("="*60)

        return ExitCode.SUCCESS

    finally:
        db.close()


def create_browser_options():
    """
    Creates and configures Chrome options for headless operation.
    Returns configured ChromeOptions object.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")  # Modern headless mode
    chrome_options.add_argument("--disable-gpu")   # Disable GPU hardware acceleration
    chrome_options.add_argument("--no-sandbox")    # Bypass OS security model
    chrome_options.add_argument("--window-size=1920,1080")  # Set a standard window size

    return chrome_options

def automate_website_interaction(headless=False):
    # Get subscription code first
    subscription_code = choose_subscription_code()

    # Initialize the Chrome WebDriver based on mode
    if headless:
        # Create browser options for headless operation
        browser_options = create_browser_options()
        driver = webdriver.Chrome(options=browser_options)
        print("🔇 Rulare în modul headless (fără fereastră)")
    else:
        # Use default windowed Chrome
        driver = webdriver.Chrome()
        print("🪟 Rulare în modul cu fereastră")

    # Create ElementFinder for robust element location with indexed selection
    finder = ElementFinder(driver)

    try:
        # Navigate to the website
        driver.get("https://bpsb.registo.ro/client-interface/appointment-subscription/step1")

        # Wait for and fill in the subscription code
        input_field = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/div/input"))
        )
        input_field.send_keys(subscription_code)

        # Click search button
        search_button = driver.find_element(By.XPATH,
                                            "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/div/div/button")
        search_button.click()

        time.sleep(0.5)

        # Check for subscription errors first
        has_error, error_message = check_for_subscription_error(driver)
        if has_error:
            print(f"\n❌ Eroare de abonament:")
            print(f"   {error_message}")
            print("\nCauze posibile:")
            print("• Codul de abonament este invalid sau expirat")
            print("• Nu au fost găsite abonamente active pentru acest client")
            print("• Abonamentul a fost deja utilizat complet")
            print("\nVă rugăm verificați codul de abonament și încercați din nou.")
            return

        # Get maximum reservations before proceeding
        success, max_reservations = get_max_reservations(driver)
        if not success or max_reservations == 0:
            print("\n❌ Nu s-a putut determina numărul maxim de rezervări.")
            print("De obicei aceasta înseamnă:")
            print("• Abonamentul este invalid sau inactiv")
            print("• Structura site-ului s-a schimbat")
            print("• Au apărut probleme de conexiune")
            print("\nVă rugăm verificați codul de abonament și încercați din nou.")
            return

        # Now get quantity from user based on the maximum
        quantity = get_quantity(max_reservations)

        # Click on sauna option
        sauna_option = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/button"))
        )
        sauna_option.click()

        # Define the XPaths we need
        calendar_table_xpath = "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div/div/div[1]/table/tbody"
        next_month_arrow_xpath = "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div/div/div[1]/table/thead/tr[2]/th[3]"

        # Wait for the calendar to load
        time.sleep(0.5)

        # Check current month's available dates and navigate if needed
        navigated = check_and_navigate_calendar(
            driver,
            calendar_table_xpath,
            next_month_arrow_xpath
        )

        # If we navigated, wait for new calendar to load
        if navigated:
            time.sleep(0.5)

        # Now get available dates from the current view
        available_dates = get_available_dates(driver, calendar_table_xpath)

        if available_dates:
            print("\nDate disponibile găsite:")
            for i, date_info in enumerate(available_dates, 1):
                day_name = get_day_name_ro(date_info['date'])
                print(f"{i}. {date_info['date']} ({day_name})")

            # Let user choose a date
            while True:
                try:
                    choice = int(input("\nVă rugăm selectați numărul datei: "))
                    if 1 <= choice <= len(available_dates):
                        selected_date = available_dates[choice - 1]
                        day_name = get_day_name_ro(selected_date['date'])
                        print(f"\nData selectată: {selected_date['date']} ({day_name})")
                        # Click the selected date
                        selected_date['element'].click()
                        break
                    print("Selecție invalidă. Vă rugăm încercați din nou.")
                except ValueError:
                    print("Vă rugăm introduceți un număr valid.")
        else:
            print("Nu s-au găsit date disponibile în calendar.")

        remaining_reservations = get_remaining_reservations(driver)
        # print(f"\nYou have {remaining_reservations} reservations remaining")

        # Get available time slots with their capacity
        available_slots = get_available_timeslots(driver)

        if not available_slots:
            print("Nu s-au găsit intervale orare pentru această dată.")
            return

        print("\nIntervale orare disponibile:")
        for slot in available_slots:
            print("\n--------------------------------")
            print(f"{slot['number']}. {slot['text']}")
            print("--------------------------------")
            # print(f"Places available: {slot['available_places']}")

        # Let user select multiple slots at once
        selected_slots = select_multiple_slots(available_slots, quantity)

        # Validate the selections
        is_valid, message = validate_slot_selections(selected_slots, quantity, remaining_reservations)
        #
        if not is_valid:
            print(f"\nEroare: {message}")
            print("Vă rugăm rulați scriptul din nou cu selecții valide.")
            return

        # Process all selected slots
        print("\nSe procesează selecțiile dvs...")
        
        # Track processed slot numbers to detect when we need fresh element references
        processed_slot_numbers = []
        
        for i, slot in enumerate(selected_slots):
            is_last_slot = (i == len(selected_slots) - 1)  # Check if this is the last slot
            slot_number = slot['number']
            
            try:
                # If we've already processed this slot number, we need to re-fetch to avoid stale elements
                if slot_number in processed_slot_numbers:
                    print(f"Se reîmprospătează sloturile pentru selecția duplicată (slot {slot_number})")
                    fresh_slots = get_available_timeslots(driver)
                    if fresh_slots and slot_number <= len(fresh_slots):
                        slot = fresh_slots[slot_number - 1]  # Get fresh element reference
                    else:
                        print(f"Eroare: Nu s-a putut reîmprospăta slotul {slot_number}")
                        continue
                
                process_slot_selection(driver, finder, slot, is_last_slot)
                processed_slot_numbers.append(slot_number)

                # Add a longer pause between slot selections
                if not is_last_slot:
                    time.sleep(0.5)  # Give more time between selections

            except Exception as e:
                print(f"Nu s-a putut procesa slotul {i+1}. Eroare: {str(e)}")
                print("Se oprește procesul...")
                return

        print("\nToate sloturile au fost procesate cu succes!")
        
        # Post-booking verification (if credentials are available)
        if has_credentials():
            print("\n" + "="*50)
            print("Se verifică rezervarea în sistem...")
            print("="*50)
            
            # Get the booked time slot info from the last processed slot
            last_slot = selected_slots[-1] if selected_slots else None
            booked_time = ""
            if last_slot and 'text' in last_slot:
                # Extract time range from slot text (e.g., "Grupa 10:30 - 14:00")
                slot_text = last_slot['text']
                # Try to extract the time portion
                time_match = re.search(r'(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})', slot_text)
                if time_match:
                    booked_time = time_match.group(1)
            
            # Get the selected date (captured earlier in the flow)
            booked_date = selected_date.get('date', '') if selected_date else ''
            
            success, details = verify_booking(driver, finder, booked_date, booked_time)
            
            if success:
                print(f"\n✓ Verificare completă: Rezervarea a fost confirmată în sistem!")
            else:
                print(f"\n⚠ Verificare incompletă: {details.get('error', 'Eroare necunoscută')}")
                print("Vă rugăm verificați manual pe site.")
        else:
            print("\n💡 Sfat: Pentru verificare automată a rezervării, adăugați")
            print("   NEPTUN_EMAIL și NEPTUN_PASSWORD în fișierul .env")

    except TimeoutException as e:
        print("Timp expirat în așteptarea elementului:", str(e))
    except Exception as e:
        print("A apărut o eroare:", str(e))
    finally:
        driver.quit()

# =============================================================================
# SUBSCRIPTION CODE LOADER
# =============================================================================

def load_subscription_codes():
    """
    Load subscription codes from NEPTUN_SUBSCRIPTIONS environment variable.
    
    Format: 'code1:name1,code2:name2'
    Example: '5642ece785:Kicky,3adc06c0e8:Adrian'
    
    Returns a list of dicts with 'code' and 'name' keys.
    """
    env_value = os.getenv('NEPTUN_SUBSCRIPTIONS', '').strip()
    
    if not env_value:
        return []
    
    # Remove surrounding quotes if present
    if env_value and env_value[0] in "'\"" and env_value[-1] in "'\"":
        env_value = env_value[1:-1]
    
    codes = []
    for pair in env_value.split(','):
        pair = pair.strip()
        if ':' in pair:
            code, name = pair.split(':', 1)
            code = code.strip()
            name = name.strip()
            if code and name:
                codes.append({'code': code, 'name': name})
    
    return codes


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    """Main entry point with mode selection and proper exit codes."""
    parser = argparse.ArgumentParser(
        description='Neptune Sauna Booking Script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python neptun.py                    # Interactive booking (windowed)
  python neptun.py --headless         # Interactive booking (headless)
  python neptun.py --status           # View current appointments
  python neptun.py --delete           # Delete appointments interactively
  python neptun.py --collect          # Collect availability data (all subscriptions)
  python neptun.py --collect -s CODE  # Collect for specific subscription
  python neptun.py --collect -v       # Collect with verbose output
  python neptun.py --trends           # Show availability trends (30 days)
  python neptun.py --trends --trends-days 7  # Show trends for last 7 days

Exit codes:
  0  - Success
  1  - Invalid subscription
  2  - No availability
  3  - Booking failed
  4  - Network error
  5  - Element not found
  6  - Timeout
  99 - Unknown error
        '''
    )
    parser.add_argument('--headless', action='store_true',
                       help='Run in headless mode (no browser window)')
    parser.add_argument('--status', action='store_true',
                       help='View current appointments (requires login credentials in .env)')
    parser.add_argument('--delete', action='store_true',
                       help='Delete appointments interactively (requires login credentials in .env)')
    parser.add_argument('--collect', action='store_true',
                       help='Silent data collection mode (for cron jobs)')
    parser.add_argument('--all', action='store_true',
                       help='Collect data for all subscriptions in input.csv')
    parser.add_argument('-s', '--subscription', type=str,
                       help='Specific subscription code to use')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--db', type=str, default=DB_FILE,
                       help=f'Database file path (default: {DB_FILE})')
    parser.add_argument('--trends', action='store_true',
                       help='Show availability trends and analytics')
    parser.add_argument('--trends-days', type=int, default=30,
                       help='Number of days for trend analysis (default: 30)')

    args = parser.parse_args()

    # Handle status and delete modes (separate flow, no database needed)
    if args.status:
        exit_code = run_status_mode(headless=args.headless)
        sys.exit(exit_code)

    if args.delete:
        exit_code = run_delete_mode(headless=args.headless)
        sys.exit(exit_code)

    if args.trends:
        exit_code = run_trends_mode(days=args.trends_days, db_path=args.db)
        sys.exit(exit_code)

    # Collect mode implies headless
    if args.collect:
        args.headless = True

    # Initialize database and logger
    db = None
    logger = None
    driver = None
    exit_code = ExitCode.SUCCESS

    try:
        db = DatabaseManager(args.db)
        logger = NeptunLogger(db, verbose=args.verbose or not args.collect)

        # Create session
        mode = 'collect' if args.collect else ('headless' if args.headless else 'interactive')
        session_id = db.create_session(mode, subscription_codes=args.subscription)
        logger.set_session(session_id)

        # Initialize browser
        if args.headless:
            browser_options = create_browser_options()
            driver = webdriver.Chrome(options=browser_options)
            if not args.collect:
                logger.info("Rulare în modul headless (fără fereastră)", "Running in headless mode")
        else:
            driver = webdriver.Chrome()
            logger.info("Rulare în modul cu fereastră", "Running in windowed mode")

        # Create helper objects
        finder = ElementFinder(driver, logger)
        verifier = StateVerifier(driver, logger, finder)

        if args.collect:
            # =========================
            # DATA COLLECTION MODE
            # =========================
            collector = AvailabilityCollector(driver, db, logger, finder, verifier)
            collector.set_session(session_id)

            # Determine which codes to collect
            if args.subscription:
                # Single subscription
                codes = [{'code': args.subscription, 'name': 'CLI'}]
            else:
                # All subscriptions from CSV
                codes = load_subscription_codes()
                if not codes:
                    logger.error(
                        "Nu s-au găsit coduri de abonament în input.csv",
                        "No subscription codes found in input.csv"
                    )
                    exit_code = ExitCode.INVALID_SUBSCRIPTION
                    raise SystemExit(exit_code)

            logger.info(
                f"Se colectează date pentru {len(codes)} abonam(ent/e)",
                f"Collecting data for {len(codes)} subscription(s)"
            )

            exit_code = collector.collect_all_subscriptions(codes)

        else:
            # =========================
            # INTERACTIVE BOOKING MODE
            # =========================
            # Use the original interactive flow (for now)
            # This could be refactored to use the new classes later
            automate_website_interaction(headless=args.headless)
            exit_code = ExitCode.SUCCESS

    except InvalidSubscriptionError as e:
        if logger:
            logger.error(f"Abonament invalid: {e}", f"Invalid subscription: {e}")
        exit_code = ExitCode.INVALID_SUBSCRIPTION

    except BookingError as e:
        if logger:
            logger.error(f"Eroare la rezervare: {e}", f"Booking error: {e}")
        exit_code = ExitCode.BOOKING_FAILED

    except ElementNotFoundError as e:
        if logger:
            logger.error(f"Element negăsit: {e}", f"Element not found: {e}")
        exit_code = ExitCode.ELEMENT_NOT_FOUND

    except TimeoutException as e:
        if logger:
            logger.error(f"Timeout: {e}", f"Timeout: {e}")
        exit_code = ExitCode.TIMEOUT

    except KeyboardInterrupt:
        print("\nOprire solicitată de utilizator.")
        exit_code = ExitCode.UNKNOWN_ERROR

    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else ExitCode.UNKNOWN_ERROR

    except Exception as e:
        if logger:
            logger.error(f"Eroare neașteptată: {e}", f"Unexpected error: {e}", error=e)
        else:
            print(f"Eroare neașteptată: {e}")
        exit_code = ExitCode.UNKNOWN_ERROR

    finally:
        # Cleanup
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

        if db and logger:
            actions, errors = logger.get_stats()
            db.end_session(session_id, exit_code, actions, errors)
            db.close()

        # Print summary in collect mode
        if args.collect and logger:
            actions, errors = logger.get_stats()
            summary = f"Session complete: {actions} actions, {errors} errors, exit code {exit_code}"
            print(summary)

    return exit_code


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
