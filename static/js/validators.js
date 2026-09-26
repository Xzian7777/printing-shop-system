/**
 * Kez C-Em Zek Web System - Input Validation Helper
 */

const AppValidators = {
    // 1. Required Field
    isRequired(value, fieldName = 'field') {
        if (!value || value.trim() === '') {
            return `Hindi pwedeng iwang blangko ang ${fieldName}.`;
        }
        return null;
    },

    // 2 & 3. Name Validation (Min: 2, Max: 50, Alphabets only)
    validateName(value) {
        if (!value || value.trim() === '') {
            return 'Required ang buong pangalan.';
        }
        const trimmed = value.trim();
        if (trimmed.length < 2) {
            return 'Ang pangalan ay dapat higit sa 1 character.';
        }
        if (trimmed.length > 50) {
            return 'Ang pangalan ay hindi pwedeng lumagpas sa 50 characters.';
        }
        const nameRegex = /^[a-zA-Z\s\-\.\ñ\Ñ]+$/;
        if (!nameRegex.test(trimmed)) {
            return 'Bawal na character sa pangalan. MGA LETRA LANG.';
        }
        return null;
    },

    // 4. Email Validation
    validateEmail(value) {
        if (!value || value.trim() === '') {
            return 'Required ang email address.';
        }
        const emailRegex = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
        if (!emailRegex.test(value.trim())) {
            return 'Maglagay ng tamang email format (hal. name@domain.com).';
        }
        return null;
    },

    // 5. Contact Number (11-digit PH mobile: 09XXXXXXXXX)
    validateContactNumber(value) {
        if (!value || value.trim() === '') {
            return 'Required ang contact number.';
        }
        const phoneRegex = /^09\d{9}$/;
        if (!phoneRegex.test(value.trim())) {
            return 'Dapat ay 11-digit PH mobile number (hal. 09123456789).';
        }
        return null;
    },

    // 6. Number Field Validation (Qty / Price)
    validateNumber(value, min = 1, max = 1000, fieldName = 'Bilang') {
        if (!value || value.toString().trim() === '') {
            return `Required ang ${fieldName}.`;
        }
        const num = Number(value);
        if (isNaN(num)) {
            return 'Maglagay ng tamang numero lang.';
        }
        if (min !== null && num < min) {
            return `Ang ${fieldName} ay hindi pwedeng mababa sa ${min}.`;
        }
        if (max !== null && num > max) {
            return `Ang ${fieldName} ay hindi pwedeng lumagpas sa ${max}.`;
        }
        return null;
    },

    // 7. Password Validation (Min 6, Max 32)
    validatePassword(value) {
        if (!value) {
            return 'Required ang password.';
        }
        if (value.length < 6) {
            return 'Ang password ay dapat may minimum na 6 characters.';
        }
        if (value.length > 32) {
            return 'Ang password ay hindi pwedeng lumagpas sa 32 characters.';
        }
        return null;
    },

    // 8. Date Validation (Not past date)
    validateDate(dateString) {
        if (!dateString) {
            return 'Mangyaring pumili ng petsa.';
        }
        const selectedDate = new Date(dateString);
        const today = new Date();
        today.setHours(0, 0, 0, 0);

        if (selectedDate < today) {
            return 'Hindi pwedeng lumipas na petsa ang piliin.';
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