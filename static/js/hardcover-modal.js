/**
 * Hardcover Edition Picker Modal
 * Allows users to select which edition to use when linking a book to Hardcover.
 */

// Modal state
let hardcoverModalState = {
    absId: null,
    bookData: null,
    selectedEditionId: null
};

function linkHardcover(event) {
    event.stopPropagation();
    hardcoverModalState.absId = event.currentTarget.dataset.absId;
    hardcoverModalState.bookData = null;
    hardcoverModalState.selectedEditionId = null;
    openHardcoverModal();
    autoResolveBook();
}

function openHardcoverModal() {
    document.getElementById('hardcover-modal').style.display = 'flex';
    showHcState('loading');
}

function closeHardcoverModal() {
    document.getElementById('hardcover-modal').style.display = 'none';
}

function showHcState(state) {
    ['loading', 'found', 'manual', 'error'].forEach(function(s) {
        document.getElementById('hc-' + s).style.display = (s === state) ? 'block' : 'none';
    });
    document.getElementById('hc-link-btn').disabled = (state !== 'found');
}

async function autoResolveBook() {
    showHcState('loading');
    try {
        const resp = await fetch('/api/hardcover/resolve?abs_id=' + hardcoverModalState.absId);
        const data = await resp.json();
        if (data.found) {
            displayBookWithEditions(data);
        } else {
            showHcState('manual');
        }
    } catch (err) {
        showHcState('manual');
    }
}

function showManualInput() {
    showHcState('manual');
    document.getElementById('hc-input').value = '';
    document.getElementById('hc-input').focus();
}

async function resolveManualInput() {
    const input = document.getElementById('hc-input').value.trim();
    if (!input) return;
    showHcState('loading');
    try {
        const resp = await fetch('/api/hardcover/resolve?abs_id=' + hardcoverModalState.absId + '&input=' + encodeURIComponent(input));
        const data = await resp.json();
        if (data.found) {
            displayBookWithEditions(data);
        } else {
            document.getElementById('hc-error-msg').textContent = data.message || 'Book not found';
            showHcState('error');
        }
    } catch (err) {
        document.getElementById('hc-error-msg').textContent = 'Search failed';
        showHcState('error');
    }
}

function displayBookWithEditions(data) {
    hardcoverModalState.bookData = data;
    document.getElementById('hc-title').textContent = data.title || 'Unknown';
    document.getElementById('hc-author').textContent = data.author || 'Unknown';

    const container = document.getElementById('hc-editions');
    container.replaceChildren();

    if (!data.editions || data.editions.length === 0) {
        const p = document.createElement('p');
        p.className = 'hc-text-muted';
        p.textContent = 'No editions found for this book.';
        container.appendChild(p);
        document.getElementById('hc-link-btn').disabled = true;
        showHcState('found');
        return;
    }

    data.editions.forEach(function(ed, idx) {
        const div = document.createElement('div');
        div.className = 'hc-edition-option' + (idx === 0 ? ' selected' : '');
        div.dataset.editionId = ed.id;
        div.dataset.pages = ed.pages || '';
        div.dataset.audioSeconds = ed.audio_seconds || '';
        div.onclick = function() { selectEdition(div); };

        const formatSpan = document.createElement('span');
        formatSpan.className = 'hc-edition-format';
        formatSpan.textContent = ed.format || 'Unknown';

        const detailsSpan = document.createElement('span');
        detailsSpan.className = 'hc-edition-details';
        detailsSpan.textContent = formatEditionDetails(ed);

        div.appendChild(formatSpan);
        div.appendChild(detailsSpan);
        container.appendChild(div);

        if (idx === 0) {
            hardcoverModalState.selectedEditionId = ed.id;
        }
    });

    document.getElementById('hc-link-btn').disabled = false;
    showHcState('found');
}

function formatEditionDetails(ed) {
    var parts = [];
    if (ed.audio_seconds && ed.audio_seconds > 0) {
        var hours = Math.floor(ed.audio_seconds / 3600);
        var mins = Math.floor((ed.audio_seconds % 3600) / 60);
        parts.push(hours + 'h ' + mins + 'm');
    } else if (ed.pages && ed.pages > 0) {
        parts.push(ed.pages + ' pp');
    }
    if (ed.year) {
        parts.push(ed.year);
    }
    return parts.join('  ·  ') || '—';
}

function selectEdition(div) {
    document.querySelectorAll('.hc-edition-option').forEach(function(el) {
        el.classList.remove('selected');
    });
    div.classList.add('selected');
    hardcoverModalState.selectedEditionId = div.dataset.editionId;
}

async function linkSelectedEdition() {
    const data = hardcoverModalState.bookData;
    const editionId = hardcoverModalState.selectedEditionId;
    const edition = data.editions.find(function(e) { return e.id == editionId; });

    try {
        const resp = await fetch('/link-hardcover/' + hardcoverModalState.absId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                book_id: data.book_id,
                edition_id: editionId,
                pages: edition ? edition.pages : null,
                audio_seconds: edition ? edition.audio_seconds : null,
                title: data.title,
                slug: data.slug
            })
        });

        if (resp.ok) {
            closeHardcoverModal();
            location.reload();
        } else {
            document.getElementById('hc-error-msg').textContent = 'Failed to link book';
            showHcState('error');
        }
    } catch (err) {
        document.getElementById('hc-error-msg').textContent = 'Failed to link book';
        showHcState('error');
    }
}
