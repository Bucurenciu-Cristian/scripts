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
            "css": "form div input[type='text'], form input[type='text'], form input[type='search']",
            "xpath": "//form//input[@type='text' or @type='search']",
            "text": None,
            "description": "Subscription code input field",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/div/input
        },
        "search_button": {
            "css": "form div div button, form button[type='submit'], form .btn",
            "xpath": "//form//button[contains(@class, 'btn')]",
            "text": "Cauta",
            "description": "Search/submit button",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/div/div/button
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
            "css": "form button, form .btn",
            "xpath": "//form/button[contains(@class, 'btn')]",
            "text": "Sauna",
            "description": "Sauna service selection button",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/button
        },
        "calendar_table": {
            "css": ".datepicker table tbody, table.table tbody, table tbody",
            "xpath": "//table[contains(@class, 'datepicker') or contains(@class, 'table')]//tbody",
            "text": None,
            "description": "Calendar date picker table body",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div/div/div[1]/table/tbody
        },
        "calendar_header": {
            "css": "table thead tr th:nth-child(2), .datepicker-switch",
            "xpath": "//table/thead/tr[1]/th[2]",
            "text": None,
            "description": "Calendar month/year header",
        },
        "next_month_arrow": {
            "css": "table thead tr th.next, th.next, .datepicker th.next",
            "xpath": "//table/thead/tr[2]/th[3]",
            "text": ">",
            "description": "Next month navigation arrow",
            # Legacy: /html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div/div/div[1]/table/thead/tr[2]/th[3]
        },
        "time_slot": {
            "css": ".alert-outline-primary, .time-slot, .slot-available, [class*='alert'][class*='primary']",
            "xpath": "//div[contains(@class, 'alert') and contains(@class, 'primary')]",
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
        "appointment_list_item": {
            "css": ".appointment, .booking-item, tr.appointment, .reservation-item, table tbody tr",
            "xpath": "//*[contains(@class, 'appointment') or contains(@class, 'booking') or contains(@class, 'reservation')]",
            "text": None,
            "description": "Appointment/booking list items",
        },
        "appointment_date": {
            "css": ".appointment-date, .date, td.date",
            "xpath": "//*[contains(@class, 'date')]",
            "text": None,
            "description": "Appointment date element",
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

    def __init__(self, driver, db_manager, logger, element_finder, verifier):
        self.driver = driver
        self.db = db_manager
        self.logger = logger
        self.finder = element_finder
        self.verifier = verifier
        self.session_id = None

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
        Returns number of slots collected.
        """
        collected_count = 0

        # Navigate to the booking page
        self.driver.get("https://bpsb.registo.ro/client-interface/appointment-subscription/step1")
        time.sleep(1)

        # Enter subscription code
        try:
            self.finder.input_text("subscription_input", code)
            self.finder.wait_and_click("search_button")
            time.sleep(0.5)
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
            time.sleep(0.5)
        except Exception as e:
            if self.logger:
                self.logger.error(f"Nu s-a putut selecta opțiunea", f"Failed to click sauna option: {e}")
            raise

        # Get available dates from calendar
        is_loaded, _ = self.verifier.verify_calendar_loaded()
        if not is_loaded:
            raise BookingError("Calendar failed to load")

        # Extract dates - just get the date strings first, not element references
        available_dates = self._extract_available_dates()
        date_strings = [d['date'] for d in available_dates]

        # Process each date by re-fetching elements each time (to avoid stale references)
        for date_str in date_strings:
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
                        self.logger.warning(
                            f"Nu s-a găsit data {date_str}",
                            f"Date {date_str} not found in calendar"
                        )
                    continue

                # Click on the date to see slots
                date_element.click()
                time.sleep(0.5)

                # Get slots for this date
                slots = self._extract_slots_for_date()

                for slot in slots:
                    self.db.log_availability(
                        self.session_id,
                        code,
                        date_str,
                        slot['time'],
                        slot['available'],
                        subscription_name=name
                    )
                    collected_count += 1

                if self.logger and slots:
                    self.logger.debug(
                        f"Colectat {len(slots)} sloturi pentru {date_str}",
                        f"Collected {len(slots)} slots for {date_str}"
                    )

            except StaleElementReferenceException:
                # If still stale, try once more with fresh fetch
                if self.logger:
                    self.logger.warning(
                        f"Element învechit pentru {date_str}, se reîncearcă",
                        f"Stale element for {date_str}, retrying"
                    )
                time.sleep(0.5)
                continue

            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        f"Nu s-au putut colecta sloturile pentru {date_str}",
                        f"Failed to collect slots for {date_str}: {e}"
                    )
                continue

        return collected_count

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

        try:
            slot_elements, _ = self.finder.find_all("time_slot")

            for slot_element in slot_elements:
                slot_text = slot_element.text.strip()

                # Skip "no slots available" messages
                if "Nu au fost gasite" in slot_text or not slot_text:
                    continue

                available_places = 0

                # Parse availability from text
                for line in slot_text.split('\n'):
                    if "Locuri disponibile:" in line:
                        try:
                            available_places = int(line.split(':')[1].strip())
                        except (ValueError, IndexError):
                            pass
                        break

                # Try to extract time from slot text (first line usually)
                time_str = slot_text.split('\n')[0].strip() if slot_text else "Unknown"

                slots.append({
                    "time": time_str,
                    "available": available_places,
                    "text": slot_text
                })

        except Exception as e:
            # No slots found is not an error - it's just an empty day
            pass

        return slots

    def set_session(self, session_id):
        """Set the session ID for logging."""
        self.session_id = session_id


# =============================================================================
# HELPER FUNCTIONS (ORIGINAL - kept for backward compatibility)
# =============================================================================

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
    Read subscription codes from input.csv and help user choose between available codes.
    """
    try:
        # Try to read subscription codes from CSV file
        codes = []
        with open('input.csv', 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if 'code' in row and 'name' in row and row['code'].strip() and row['name'].strip():
                    codes.append({
                        'code': row['code'].strip(),
                        'name': row['name'].strip()
                    })
        
        if not codes:
            raise ValueError("Nu s-au găsit coduri valide în input.csv")
        
        # Display available codes
        print("\\nCoduri de abonament disponibile:")
        for i, code_info in enumerate(codes, 1):
            print(f"{i}. {code_info['code']} {code_info['name']}")
        
        # Get user selection
        while True:
            try:
                choice = input(f"\\nVă rugăm selectați un cod (1-{len(codes)}): ")
                choice_num = int(choice)
                
                if 1 <= choice_num <= len(codes):
                    selected_code = codes[choice_num - 1]['code']
                    print(f"Cod selectat: {selected_code} ({codes[choice_num - 1]['name']})")
                    return selected_code
                else:
                    print(f"Alegere invalidă. Vă rugăm selectați între 1 și {len(codes)}.")
                    
            except ValueError:
                print("Vă rugăm introduceți un număr valid.")
    
    except FileNotFoundError:
        print("\\n❌ Fișierul input.csv nu a fost găsit!")
        print("Creați fișierul input.csv cu următorul format:")
        print("code,name")
        print("5642ece785,Kicky")
        print("3adc06c0e8,Adrian")
        print("\\nSe folosesc codurile implicite pentru această sesiune...")
        
        # Fallback to hardcoded values
        return choose_subscription_code_fallback()
    
    except Exception as e:
        print(f"\\n❌ Eroare la citirea fișierului input.csv: {str(e)}")
        print("Se folosesc codurile implicite pentru această sesiune...")
        
        # Fallback to hardcoded values
        return choose_subscription_code_fallback()


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
        print(f"Nu s-a putut extrage antetul calendarului, se folosește luna/anul curent:")

    # Find all date cells
    date_cells = table.find_elements(By.TAG_NAME, "td")

    for cell in date_cells:
        # Check if the cell is not disabled
        if "disabled" not in cell.get_attribute("class"):
            date_text = cell.text
            if date_text.strip():  # Ensure the cell contains a date
                # Format as DD-MM-YYYY
                day = date_text.strip().zfill(2)  # Pad single digits with leading zero
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
                print(f"{i}. {date_info['date']}")

            # Let user choose a date
            while True:
                try:
                    choice = int(input("\nVă rugăm selectați numărul datei: "))
                    if 1 <= choice <= len(available_dates):
                        selected_date = available_dates[choice - 1]
                        print(f"\nData selectată: {selected_date['date']}")
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
    Load subscription codes from input.csv file.
    Returns a list of dicts with 'code' and 'name' keys.
    """
    codes = []
    try:
        with open('input.csv', 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if 'code' in row and 'name' in row and row['code'].strip() and row['name'].strip():
                    codes.append({
                        'code': row['code'].strip(),
                        'name': row['name'].strip()
                    })
    except FileNotFoundError:
        pass  # Return empty list
    except Exception:
        pass
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
  python neptun.py --collect          # Collect availability data (all subscriptions)
  python neptun.py --collect -s CODE  # Collect for specific subscription
  python neptun.py --collect -v       # Collect with verbose output

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

    args = parser.parse_args()

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
