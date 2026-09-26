import re
from datetime import datetime

class ServerValidators:
    @staticmethod
    def validate_name(name):
        if not name or not str(name).strip():
            return False, "Hindi pwedeng blangko ang pangalan."
        cleaned = str(name).strip()
        if len(cleaned) < 2:
            return False, "Ang pangalan ay dapat higit sa 1 character."
        if len(cleaned) > 50:
            return False, "Ang pangalan ay hindi pwedeng lumagpas sa 50 characters."
        if not re.match(r"^[a-zA-Z\s\-\.\ñ\Ñ]+$", cleaned):
            return False, "Bawal na characters sa pangalan. Mga letra lamang."
        return True, None

    @staticmethod
    def validate_email(email):
        if not email or not str(email).strip():
            return False, "Required ang email address."
        email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_regex, str(email).strip()):
            return False, "Maglagay ng tamang email format (hal. name@domain.com)."
        return True, None

    @staticmethod
    def validate_contact(contact):
        if not contact or not str(contact).strip():
            return False, "Required ang contact number."
        cleaned = str(contact).strip()
        if not re.match(r"^09\d{9}$", cleaned):
            return False, "Dapat ay 11-digit PH mobile number (hal. 09123456789)."
        return True, None

    @staticmethod
    def validate_number(val, min_val=1, max_val=1000, name="Quantity"):
        if val is None or str(val).strip() == "":
            return False, f"Required ang {name}."
        try:
            num = float(val)
            if num < min_val:
                return False, f"Ang {name} ay hindi pwedeng mababa sa {min_val}."
            if num > max_val:
                return False, f"Ang {name} ay hindi pwedeng lumagpas sa {max_val}."
            return True, None
        except (ValueError, TypeError):
            return False, f"Invalid number format sa {name}."

    @staticmethod
    def validate_password(password):
        if not password:
            return False, "Required ang password."
        if len(password) < 6:
            return False, "Ang password ay dapat may minimum na 6 characters."
        if len(password) > 32:
            return False, "Ang password ay hindi pwedeng lumagpas sa 32 characters."
        return True, None

    @staticmethod
    def validate_date(date_str):
        if not date_str:
            return False, "Mangyaring pumili ng petsa."
        try:
            selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            today = datetime.now().date()
            if selected_date < today:
                return False, "Hindi pwedeng lumipas na petsa ang piliin."
            return True, None
        except ValueError:"""
Kez C-Em Zek Web System - Server-Side Python Validation Module
"""
import re
from datetime import datetime

class ServerValidators:

    @staticmethod
    def validate_name(name):
        if not name or not str(name).strip():
            return False, "Full name is required."
        trimmed = str(name).strip()
        if len(trimmed) < 2:
            return False, "Name must be at least 2 characters long."
        if len(trimmed) > 50:
            return False, "Name cannot exceed 50 characters."
        if not re.match(r"^[a-zA-Z\s\-\.\ñ\Ñ]+$", trimmed):
            return False, "Name contains invalid characters. Letters and spaces only."
        return True, None

    @staticmethod
    def validate_email(email):
        if not email or not str(email).strip():
            return False, "Email address is required."
        trimmed = str(email).strip()
        email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_regex, trimmed):
            return False, "Invalid email address format."
        return True, None

    @staticmethod
    def validate_contact(contact):
        if not contact or not str(contact).strip():
            return False, "Contact number is required."
        trimmed = str(contact).strip()
        if not re.match(r"^09\d{9}$", trimmed):
            return False, "Contact number must be an 11-digit mobile number starting with 09."
        return True, None

    @staticmethod
    def validate_number(val, min_val=1, max_val=1000, name="Quantity"):
        if val is None or str(val).strip() == "":
            return False, f"{name} is required."
        try:
            num = float(val)
            if min_val is not None and num < min_val:
                return False, f"{name} cannot be less than {min_val}."
            if max_val is not None and num > max_val:
                return False, f"{name} cannot exceed {max_val}."
            return True, None
        except (ValueError, TypeError):
            return False, f"Please enter a valid numeric value for {name}."

    @staticmethod
    def validate_password(password):
        if not password:
            return False, "Password is required."
        if len(password) < 6:
            return False, "Password must be at least 6 characters long."
        if len(password) > 32:
            return False, "Password cannot exceed 32 characters."
        return True, None

    @staticmethod
    def validate_date(date_str):
        if not date_str or not str(date_str).strip():
            return False, "Date selection is required."
        try:
            selected_date = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
            today = datetime.now().date()
            if selected_date < today:
                return False, "Selected date cannot be in the past."
            return True, None
        except ValueError:
            return False, "Invalid date format. Use YYYY-MM-DD."
            return False, "Invalid date format. Gagamit dapat ng YYYY-MM-DD."