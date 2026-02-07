/**
 * Storyteller Legacy Link Fix Modal
 * Handles searching and linking Storyteller books to existing ABS books.
 */

let currentAbsId = null;

function openStorytellerModal(absId, title) {
    currentAbsId = absId;
    document.getElementById('st-modal-title').textContent = `Link Storyteller: ${title}`;
    document.getElementById('st-modal').classList.remove('hidden');
    document.getElementById('st-search-input').value = title; // Pre-fill with title
    document.getElementById('st-search-input').focus();
    document.getElementById('st-results').innerHTML = ''; // Clear results
    
    // Auto-search if title is present
    if (title) searchStoryteller();
}

function closeStorytellerModal() {
    document.getElementById('st-modal').classList.add('hidden');
    currentAbsId = null;
}

async function searchStoryteller() {
    const query = document.getElementById('st-search-input').value;
    if (!query) return;

    const resultsDiv = document.getElementById('st-results');
    resultsDiv.innerHTML = '<div class="st-loading">Searching...</div>';

    try {
        const response = await fetch(`/api/storyteller/search?q=${encodeURIComponent(query)}`);
        const books = await response.json();

        resultsDiv.innerHTML = '';
        if (books.length === 0) {
            resultsDiv.innerHTML = '<div class="st-no-results">No books found</div>';
            return;
        }

        books.forEach(book => {
            const card = document.createElement('div');
            card.className = 'st-result-card';
            card.innerHTML = `
                <div class="st-card-info">
                    <div class="st-card-title">${book.title}</div>
                    <div class="st-card-author">${book.authors.join(', ')}</div>
                </div>
                <button class="action-btn success" onclick="linkStoryteller('${book.uuid}')">Link</button>
            `;
            resultsDiv.appendChild(card);
        });

    } catch (e) {
        resultsDiv.innerHTML = `<div class="st-error">Error: ${e.message}</div>`;
    }
}

async function linkStoryteller(uuid) {
    if (!currentAbsId) return;
    
    const resultsDiv = document.getElementById('st-results');
    resultsDiv.innerHTML = '<div class="st-loading">Linking and downloading...</div>';

    try {
        const response = await fetch(`/api/storyteller/link/${currentAbsId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ uuid: uuid })
        });

        if (response.ok) {
            window.location.reload();
        } else {
            const err = await response.json();
            throw new Error(err.error || 'Failed to link');
        }
    } catch (e) {
        resultsDiv.innerHTML = `<div class="st-error">Link Failed: ${e.message}</div>`;
    }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    // Close on click outside
    document.getElementById('st-modal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('st-modal')) {
            closeStorytellerModal();
        }
    });

    // Enter key in search
    document.getElementById('st-search-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            searchStoryteller();
        }
    });
});
