# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python web automation script for booking sauna reservations on the BPSB system. The script uses Selenium WebDriver to interact with a booking website, allowing users to select subscription codes, choose dates, and book multiple time slots automatically.

## Dependencies and Setup

The script requires Python with Selenium and Chrome WebDriver:
- `selenium` - for web automation
- Chrome browser with ChromeDriver installed
- Standard Python libraries: `time`, `datetime`

To run the script:
```bash
python neptun.py
```

## Architecture and Code Structure

### Main Components

**Main Entry Point (`automate_website_interaction`)**
- Orchestrates the entire booking flow
- Handles user interaction and web navigation sequence

**Slot Management Functions**
- `parse_slot_info()` - extracts availability data from time slot elements  
- `get_available_timeslots()` - finds all available booking slots
- `select_multiple_slots()` - handles user selection of multiple slots
- `process_slot_selection()` - processes individual slot booking with cart management

**Calendar Navigation**
- `check_and_navigate_calendar()` - automatically navigates to months with sufficient availability
- `get_available_dates()` - extracts available dates from calendar table
- `count_available_dates()` - counts available dates in current view

**Validation System**
- `validate_slot_selections()` - ensures selections meet all constraints (quantity, availability, remaining reservations)
- `validate_quantity()` - validates requested quantity against limits
- Multi-layer validation for user selections and system constraints

**Configuration Management**
- `choose_subscription_code()` - presents hardcoded subscription options
- `check_for_subscription_error()` - detects invalid/expired subscription codes
- `get_max_reservations()` / `get_remaining_reservations()` - extracts booking limits from UI with defensive error handling
- `create_browser_options()` - configures Chrome for headless operation

### Key Technical Details

**XPath Strategy**: Uses absolute XPaths for element location - brittle to UI changes but specific to this booking system

**State Management**: Tracks reservation limits, slot availability, and user selections throughout the booking flow

**Browser Modes**: Supports both headless and windowed Chrome operation (currently configured for windowed)

**Error Handling**: Comprehensive try-catch blocks with timeout handling for web elements and graceful subscription error detection

**User Flow**: Interactive command-line interface for subscription selection, date picking, and slot selection

## Booking Flow Architecture

1. **Initialization**: User selects subscription code from predefined options
2. **Authentication**: Enters subscription code on booking website  
3. **Error Detection**: Checks for invalid/expired subscription errors before proceeding
4. **Capacity Check**: System extracts maximum allowed reservations with defensive error handling
4. **Quantity Input**: User specifies desired number of bookings
5. **Calendar Navigation**: Automatically finds months with sufficient availability (15+ days minimum)
6. **Date Selection**: User chooses specific date from available options
7. **Slot Selection**: User selects multiple time slots with availability validation
8. **Booking Processing**: Iterates through selected slots, handling cart operations for each

## Validation Rules

- User must select exact quantity requested
- Each slot must have sufficient capacity for the requested quantity  
- Total selections cannot exceed remaining reservations
- No duplicate slot selections allowed
- Slot numbers must be within valid range

## Browser Automation Patterns

**Wait Strategies**: Uses WebDriverWait with expected conditions for reliable element interaction

**Element Interaction**: Combines clicking, text extraction, and form filling with appropriate delays

**Cart Management**: Handles multi-step cart operations - select slot, click "Selecteaza", manage cart state between selections

**Final Processing**: Special handling for the last slot selection to complete the booking flow