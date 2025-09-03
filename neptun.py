from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
from datetime import datetime, timedelta
import argparse
import csv


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
        print(f"\nSe selectează slotul: {slot['text']}")
        slot['element'].click()
        time.sleep(0.5)  # Wait for the selection to register

        # Wait for and click the "Selecteaza" button
        selecteaza_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                                        "/html/body/div[1]/div/div/div[1]/div[2]/div/div/div/div/div[2]/div/div/div/div[4]/div[2]/form/div/div[2]/button"))
        )
        selecteaza_button.click()
        print("S-a apăsat butonul 'Selectează'")
        time.sleep(0.5)  # Wait for cart to appear

        # If this is not the last slot, we need to exit the cart
        if not is_last_slot:
            close_cart_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/div[2]/div[1]/a"))
            )
            close_cart_button.click()
            print("S-a închis coșul pentru a continua selecția")
            time.sleep(0.5)  # Wait for cart to close
        else:
            # This is the last slot, so click the final button
            print("Se procesează selecția finală...")
            final_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/div[2]/div[2]/div[2]/a[2]"))
            )
            final_button.click()
            print("Selecția finală completată!")

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
                
                process_slot_selection(driver, slot, is_last_slot)
                processed_slot_numbers.append(slot_number)

                # Add a longer pause between slot selections
                if not is_last_slot:
                    time.sleep(0.5)  # Give more time between selections

            except Exception as e:
                print(f"Nu s-a putut procesa slotul {i+1}. Eroare: {str(e)}")
                print("Se oprește procesul...")
                return

        print("\nToate sloturile au fost procesate cu succes!")

    except TimeoutException as e:
        print("Timp expirat în așteptarea elementului:", str(e))
    except Exception as e:
        print("A apărut o eroare:", str(e))
    finally:
        driver.quit()

if __name__ == "__main__":
    # Set up command line argument parsing
    parser = argparse.ArgumentParser(description='Neptune Sauna Booking Script')
    parser.add_argument('--headless', action='store_true', 
                       help='Run in headless mode (no browser window)')
    
    args = parser.parse_args()
    
    # Run the automation with the specified mode
    automate_website_interaction(headless=args.headless)
