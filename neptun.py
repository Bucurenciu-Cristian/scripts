from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
from datetime import datetime, timedelta


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
        time.sleep(1)
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
        return False, f"You must select exactly {requested_quantity} slots."

    if requested_quantity > remaining_reservations:
        return False, f"You requested {requested_quantity} slots but you only have {remaining_reservations} reservations remaining."

    # Check each slot's availability
    for slot in selected_slots:
        if requested_quantity > slot["available_places"]:
            return False, f"Slot {slot['number']} only has {slot['available_places']} places available, but you requested {requested_quantity}."

    return True, "Selection is valid"

def select_multiple_slots(available_slots, quantity):
    """
    Allows user to select multiple slots at once.
    Returns list of selected slots.
    """
    selected_slots = []
    print("\nPlease enter the numbers of all slots you want to select, separated by spaces.")
    print(f"You need to select {quantity} slots.")

    while True:
        try:
            selections = input("Enter slot numbers: ").strip().split()

            # Convert to integers and validate
            slot_numbers = [int(x) for x in selections]

            # Validate quantity
            if len(slot_numbers) != quantity:
                print(f"Please select exactly {quantity} slots.")
                continue

            # Validate numbers and check for duplicates
            if len(set(slot_numbers)) != len(slot_numbers):
                print("Please don't select the same slot multiple times.")
                continue

            # Validate range
            if not all(1 <= num <= len(available_slots) for num in slot_numbers):
                print(f"Please enter numbers between 1 and {len(available_slots)}")
                continue

            # Get the selected slots
            selected_slots = [available_slots[num-1] for num in slot_numbers]
            return selected_slots

        except ValueError:
            print("Please enter valid numbers separated by spaces.")

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
        return False, f"You requested {requested_quantity} slots but you only have {remaining_reservations} reservations remaining."

    if requested_quantity > len(available_slots):
        return False, f"You requested {requested_quantity} slots but only {len(available_slots)} time slots are available."

    return True, "Quantity is valid"

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
    print(f"Found {available_count} available dates in current view")

    if available_count < minimum_days:
        try:
            # Find and click the next month arrow
            next_month_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, next_month_arrow_xpath))
            )
            next_month_button.click()
            print("Navigated to next month due to insufficient available dates")

            # Add a small delay to let the calendar update
            time.sleep(2)
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


def choose_subscription_code():
    """
    Help user choose between available subscription codes.
    """
    print("\nAvailable subscription codes:")
    print("1. 5642ece785 Kicky")
    print("2. 3adc06c0e8 Adrian")

    while True:
        choice = input("\nPlease select a code (1 or 2): ")
        if choice == "1":
            return "5642ece785"
        elif choice == "2":
            return "3adc06c0e8"
        else:
            print("Invalid choice. Please select 1 or 2.")


def get_max_reservations(driver):
    """
    Extracts the maximum number of available reservations from the span element.
    This function is called right after entering the subscription code.
    Returns the maximum number of reservations allowed.
    """
    try:
        # Wait for the span element containing the reservation info to be present
        reservation_span = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH,
                                            "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/form/button/span[2]"))
        )

        # Extract text and split by ":" to get the number
        text = reservation_span.text
        max_reservations = int(text.split(":")[1].strip())

        return max_reservations
    except Exception as e:
        print(f"Error getting maximum reservations: {str(e)}")
        return 0

def get_quantity(max_reservations):
    """
    Get the desired quantity of items from user, ensuring it doesn't exceed the maximum.
    """
    while True:
        try:
            print(f"\nYou can book up to {max_reservations} reservations.")
            quantity = int(input("How many reservations would you like to make? "))
            if 0 < quantity <= max_reservations:
                return quantity
            elif quantity <= 0:
                print("Please enter a number greater than 0.")
            else:
                print(f"You cannot book more than {max_reservations} reservations.")
        except ValueError:
            print("Please enter a valid number.")

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

    # Find all date cells
    date_cells = table.find_elements(By.TAG_NAME, "td")

    for cell in date_cells:
        # Check if the cell is not disabled
        if "disabled" not in cell.get_attribute("class"):
            date_text = cell.text
            if date_text.strip():  # Ensure the cell contains a date
                available_dates.append({
                    "date": date_text,
                    "element": cell
                })

    return available_dates


def process_slot_selection(driver, slot, is_last_slot=False):
    """
    Processes a single slot selection including all necessary button clicks.

    Parameters:
    - driver: Selenium WebDriver instance
    - slot: The slot information dictionary
    - is_last_slot: Boolean indicating if this is the last slot being processed
    """
    try:
        # Click the slot itself first
        print(f"\nSelecting slot: {slot['text']}")
        slot['element'].click()
        time.sleep(1)  # Wait for the selection to register

        # Wait for and click the "Selecteaza" button
        selecteaza_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div[4]/div[2]/form/div/div[2]/button"))
        )
        selecteaza_button.click()
        print("Clicked 'Selecteaza' button")
        time.sleep(1)  # Wait for cart to appear

        # If this is not the last slot, we need to exit the cart
        if not is_last_slot:
            close_cart_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/div[2]/div[1]/a"))
            )
            close_cart_button.click()
            print("Closed cart to continue selection")
            time.sleep(1)  # Wait for cart to close
        else:
            # This is the last slot, so click the final button
            print("Processing final selection...")
            final_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/div[2]/div[2]/div[2]/a[2]"))
            )
            final_button.click()
            print("Completed final selection!")

    except TimeoutException as e:
        print(f"Timeout error while processing slot: {str(e)}")
        raise
    except Exception as e:
        print(f"Error processing slot selection: {str(e)}")
        raise


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

def automate_website_interaction():
    # Get subscription code first
    subscription_code = choose_subscription_code()

    # Create browser options for headless operation
    # browser_options = create_browser_options()

    # Initialize the Chrome WebDriver with our options
    # driver = webdriver.Chrome(options=browser_options)
    driver = webdriver.Chrome()


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

        time.sleep(1)

        # Get maximum reservations before proceeding
        max_reservations = get_max_reservations(driver)
        if max_reservations == 0:
            print("Could not determine maximum reservations. Please check the website.")
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
        time.sleep(1)

        # Check current month's available dates and navigate if needed
        navigated = check_and_navigate_calendar(
            driver,
            calendar_table_xpath,
            next_month_arrow_xpath
        )

        # If we navigated, wait for new calendar to load
        if navigated:
            time.sleep(1)

        # Now get available dates from the current view
        available_dates = get_available_dates(driver, calendar_table_xpath)

        if available_dates:
            print("\nAvailable dates found:")
            for i, date_info in enumerate(available_dates, 1):
                print(f"{i}. {date_info['date']}")

            # Let user choose a date
            while True:
                try:
                    choice = int(input("\nPlease select a date number: "))
                    if 1 <= choice <= len(available_dates):
                        selected_date = available_dates[choice - 1]
                        print(f"\nSelected date: {selected_date['date']}")
                        # Click the selected date
                        selected_date['element'].click()
                        break
                    print("Invalid selection. Please try again.")
                except ValueError:
                    print("Please enter a valid number.")
        else:
            print("No available dates found in the calendar.")

        remaining_reservations = get_remaining_reservations(driver)
        # print(f"\nYou have {remaining_reservations} reservations remaining")

        # Get available time slots with their capacity
        available_slots = get_available_timeslots(driver)

        if not available_slots:
            print("No time slots found for this date.")
            return

        print("\nAvailable time slots:")
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
            print(f"\nError: {message}")
            print("Please run the script again with valid selections.")
            return

        # Process all selected slots
        print("\nProcessing your selections...")
        for i, slot in enumerate(selected_slots):
            is_last_slot = (i == len(selected_slots) - 1)  # Check if this is the last slot
            try:
                process_slot_selection(driver, slot, is_last_slot)

                # Add a longer pause between slot selections
                if not is_last_slot:
                    time.sleep(1)  # Give more time between selections

            except Exception as e:
                print(f"Failed to process slot {i+1}. Error: {str(e)}")
                print("Stopping the process...")
                return

        print("\nAll slots have been successfully processed!")

    except TimeoutException as e:
        print("Timeout while waiting for element:", str(e))
    except Exception as e:
        print("An error occurred:", str(e))
    finally:
        driver.quit()

if __name__ == "__main__":
    automate_website_interaction()
