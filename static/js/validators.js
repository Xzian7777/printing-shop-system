/**
 * Kez C-Em Zek Web System - Input Validation Helper
 */

const AppValidators = {
    // 1. Required Field
    isRequired(value, fieldName = 'field') {
        if (!value || value.trim() === '') {
            return `${fieldName} is required.`;
        }
        return null;
    },

    // 2 & 3. Name Validation (Min: 2, Max: 50, Alphabets only)
    validateName(value) {
        if (!value || value.trim() === '') {
            return 'Full name is required.';
        }
        const trimmed = value.trim();
        if (trimmed.length < 2) {
            return 'Name must contain at least 2 characters.';
        }
        if (trimmed.length > 50) {
            return 'Name must not exceed 50 characters.';
        }
        const nameRegex = /^[a-zA-Z\s\-\.\ñ\Ñ]+$/;
        if (!nameRegex.test(trimmed)) {
            return 'Name may contain letters only.';
        }
        return null;
    },

    // 4. Email Validation
    validateEmail(value) {
        if (!value || value.trim() === '') {
            return 'Email address is required.';
        }
        const emailRegex = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
        if (!emailRegex.test(value.trim())) {
            return 'Enter a valid email address (for example, name@domain.com).';
        }
        return null;
    },

    // 5. Contact Number (11-digit PH mobile: 09XXXXXXXXX)
    validateContactNumber(value) {
        if (!value || value.trim() === '') {
            return 'Contact number is required.';
        }
        const phoneRegex = /^09\d{9}$/;
        if (!phoneRegex.test(value.trim())) {
            return 'Enter an 11-digit Philippine mobile number (for example, 09123456789).';
        }
        return null;
    },

    // 6. Number Field Validation (Qty / Price)
    validateNumber(value, min = 1, max = 1000, fieldName = 'Bilang') {
        if (!value || value.toString().trim() === '') {
            return `${fieldName} is required.`;
        }
        const num = Number(value);
        if (isNaN(num)) {
            return 'Enter a valid number.';
        }
        if (min !== null && num < min) {
            return `${fieldName} must be at least ${min}.`;
        }
        if (max !== null && num > max) {
            return `${fieldName} must not exceed ${max}.`;
        }
        return null;
    },

    // 7. Password Validation (Min 6, Max 32)
    validatePassword(value) {
        if (!value) {
            return 'Password is required.';
        }
        if (value.length < 6) {
            return 'Password must contain at least 6 characters.';
        }
        if (value.length > 32) {
            return 'Password must not exceed 32 characters.';
        }
        return null;
    },

    // 8. Date Validation (Not past date)
    validateDate(dateString) {
        if (!dateString) {
            return 'Please select a date.';
        }
        const selectedDate = new Date(dateString);
        const today = new Date();
        today.setHours(0, 0, 0, 0);

        if (selectedDate < today) {
            return 'Please select today or a future date.';
        }
        return null;
    }
};

/**
 * Helper Function para magpakita ng red error message sa ilalim ng HTML input
 */
function showError(inputId, errorMsg) {
    const inputElem = document.getElementById(inputId);
    let errorElem = document.getElementById(`${inputId}-error`);

    if (!errorElem && inputElem) {
        errorElem = document.createElement('div');
        errorElem.id = `${inputId}-error`;
        errorElem.className = 'text-danger small mt-1 font-weight-bold';
        inputElem.parentNode.appendChild(errorElem);
    }

    if (errorMsg) {
        if (inputElem) inputElem.classList.add('is-invalid');
        if (errorElem) errorElem.innerText = errorMsg;
    } else {
        if (inputElem) inputElem.classList.remove('is-invalid');
        if (errorElem) errorElem.innerText = '';
    }
}