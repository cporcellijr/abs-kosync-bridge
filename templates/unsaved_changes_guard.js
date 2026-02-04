
// --- Unsaved Changes Guard ---
let isFormDirty = false;

function markDirty() {
    isFormDirty = true;
}

function setupDirtyCheck() {
    const inputs = document.querySelectorAll('input, select, textarea');
    inputs.forEach(input => {
        input.addEventListener('change', markDirty);
        input.addEventListener('input', markDirty);
    });

    // Warn before leaving
    window.addEventListener('beforeunload', function (e) {
        if (isFormDirty) {
            // Standard message (though most modern browsers show their own generic message)
            const msg = "You have unsaved changes. Are you sure you want to leave?";
            e.returnValue = msg;
            return msg;
        }
    });

    // Clear dirty flag on valid submit
    const form = document.querySelector('form');
    if (form) {
        form.addEventListener('submit', function () {
            isFormDirty = false;
        });
    }
}

// Initialize dirty check after DOM is ready
document.addEventListener('DOMContentLoaded', setupDirtyCheck);
