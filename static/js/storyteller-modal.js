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

        // [NEW] Always show "None" option to allow unlinking
        const noneCard = document.createElement('div');
        noneCard.className = 'st-result-card st-none-option';
        noneCard.style.border = '1px dashed #666';
        noneCard.innerHTML = `
            <div class="st-card-info">
                <div class="st-card-title">None - Do not link</div>
                <div class="st-card-author" style="font-style: italic; color: #888;">Unlink current Storyteller book</div>
            </div>
            <button class="action-btn secondary" onclick="linkStoryteller('none')">Unlink</button>
        `;
        resultsDiv.appendChild(noneCard);

        if (books.length === 0) {
            const noRes = document.createElement('div');
            noRes.className = 'st-no-results';
            noRes.textContent = 'No matching books found via search.';
            resultsDiv.appendChild(noRes);
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
