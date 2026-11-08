const $ = selector => document.querySelector(selector);
const loginSection = $('#adminLogin');
const dashboard = $('#adminDashboard');
const api = (path, options = {}) => fetch(`/api/admin/${path}`, {
  credentials: 'same-origin',
  ...options,
  headers: { 'Content-Type': 'application/json', ...options.headers },
});

function localDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function displayDate(value) {
  if (!value) return 'niciodată';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? 'necunoscut' : date.toLocaleString('ro-RO');
}

async function refreshWorkerStatus() {
  const summary = $('#workerStatusSummary');
  const details = $('#workerStatusDetails');
  const errorMessage = $('#workerStatusError');
  try {
    const response = await api('status');
    if (!response.ok) throw new Error('Nu am putut citi starea workerului.');
    const status = await response.json();
    if (!status.alive) {
      summary.textContent = 'Workerul nu a raportat recent. Verifică serviciul worker în TrueNAS.';
    } else if (status.state === 'processing') {
      summary.textContent = `Procesează ${status.current_album || 'galeria'}${status.current_file ? ` · ${status.current_file}` : ''}`;
    } else if (status.state === 'error') {
      summary.textContent = 'Workerul a întâmpinat o eroare.';
    } else {
      summary.textContent = 'Worker activ · scanare în așteptare';
    }
    const progress = Number.isFinite(status.processed) && Number.isFinite(status.total)
      ? ` · ${status.processed}/${status.total} fișiere verificate`
      : '';
    details.textContent = `Ultima scanare completă: ${displayDate(status.last_scan)}${progress}`;
    errorMessage.textContent = status.last_error ? `Ultima eroare: ${status.last_error}` : '';
    $('#workerStatus').classList.toggle('worker-status-error', !status.alive || status.state === 'error');
  } catch (error) {
    summary.textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta pentru a verifica workerul.'
      : error.message;
    details.textContent = '';
    $('#workerStatus').classList.add('worker-status-error');
  }
}

async function refreshAlbums() {
  const response = await api('albums');
  if (response.status === 401) {
    loginSection.hidden = false;
    dashboard.hidden = true;
    return;
  }
  if (!response.ok) throw new Error('Nu am putut încărca lista galeriilor.');
  const albums = await response.json();
  const list = $('#albumList');
  list.replaceChildren();
  if (!albums.length) {
    const empty = document.createElement('p');
    empty.className = 'album-empty';
    empty.textContent = 'Nu ai creat încă nicio galerie.';
    list.append(empty);
    return;
  }
  albums.forEach(album => {
    const row = document.createElement('article');
    row.className = 'album-row';
    if (album.expired) row.classList.add('album-row-expired');
    const details = document.createElement('div');
    const title = document.createElement('h3');
    title.textContent = album.title;
    const themeNames = {
      default: 'Implicită',
      wedding: 'Nuntă',
      'baptism-boy': 'Botez băiat',
      'baptism-girl': 'Botez fată',
      'prom-boy': 'Majorat băiat',
      'prom-girl': 'Majorat fată',
    };
    const metadata = document.createElement('p');
    metadata.textContent = `${album.folder} · /g/${album.slug}`;
    details.append(title, metadata);
    if (album.description) {
      const description = document.createElement('p');
      description.textContent = album.description;
      details.append(description);
    }
    const expiration = document.createElement('p');
    expiration.textContent = album.expires_at
      ? album.expired ? `Expirată: ${displayDate(album.expires_at)}` : `Expiră: ${displayDate(album.expires_at)}`
      : 'Fără expirare';
    details.append(expiration);
    const pinControl = document.createElement('div');
    pinControl.className = 'album-pin-control';
    const revealPin = document.createElement('button');
    revealPin.className = 'album-theme-save';
    revealPin.type = 'button';
    revealPin.textContent = 'Afișează PIN-ul';
    const pinValue = document.createElement('span');
    pinValue.className = 'album-pin-value';
    if (album.pin_recoverable) {
      revealPin.addEventListener('click', async () => {
        if (pinValue.textContent) {
          pinValue.textContent = '';
          revealPin.textContent = 'Afișează PIN-ul';
          return;
        }
        revealPin.disabled = true;
        try {
          const response = await api(`albums/${encodeURIComponent(album.slug)}/pin`);
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Nu am putut afișa PIN-ul.');
          pinValue.textContent = result.pin;
          revealPin.textContent = 'Ascunde PIN-ul';
        } catch (error) {
          window.alert(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message);
        } finally {
          revealPin.disabled = false;
        }
      });
    } else {
      revealPin.disabled = true;
      revealPin.textContent = 'PIN nerecuperabil';
      pinValue.textContent = 'Acest PIN a fost setat înainte de activarea afișării și nu poate fi recuperat fără schimbare.';
    }
    pinControl.append(revealPin, pinValue);
    details.append(pinControl);
    const link = document.createElement('a');
    link.className = 'album-open';
    link.href = `/g/${encodeURIComponent(album.slug)}`;
    link.textContent = 'Deschide →';
    const actions = document.createElement('div');
    actions.className = 'album-actions';
    const themeEditor = document.createElement('div');
    themeEditor.className = 'album-theme-editor';
    const themeSelect = document.createElement('select');
    themeSelect.setAttribute('aria-label', `Tema galeriei ${album.title}`);
    Object.entries(themeNames).forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.selected = value === (album.theme || 'default');
      themeSelect.append(option);
    });
    const coverSelect = document.createElement('select');
    coverSelect.setAttribute('aria-label', `Fotografia de copertă pentru ${album.title}`);
    const noCover = document.createElement('option');
    noCover.value = '';
    noCover.textContent = 'Fără copertă';
    coverSelect.append(noCover);
    (album.cover_options || []).forEach(name => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      option.selected = name === album.cover;
      coverSelect.append(option);
    });
    if (!album.cover) noCover.selected = true;
    const expirationInput = document.createElement('input');
    expirationInput.type = 'datetime-local';
    expirationInput.setAttribute('aria-label', `Data expirării pentru ${album.title}`);
    expirationInput.value = localDateTime(album.expires_at);
    expirationInput.title = 'Data și ora sunt în fusul orar local';
    const saveTheme = document.createElement('button');
    saveTheme.className = 'album-theme-save';
    saveTheme.type = 'button';
    saveTheme.textContent = 'Salvează';
    saveTheme.addEventListener('click', async () => {
      saveTheme.disabled = true;
      try {
        let expiresAt = null;
        if (expirationInput.value) {
          const parsed = new Date(expirationInput.value);
          if (Number.isNaN(parsed.getTime())) throw new Error('Data expirării nu este validă.');
          expiresAt = parsed.toISOString();
        }
        const response = await api(`albums/${encodeURIComponent(album.slug)}`, {
          method: 'PUT',
          body: JSON.stringify({
            theme: themeSelect.value,
            cover: coverSelect.value || null,
            expires_at: expiresAt,
          }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Nu am putut salva setările galeriei.');
        saveTheme.textContent = 'Salvat';
        await refreshAlbums();
        window.setTimeout(() => { saveTheme.textContent = 'Salvează'; }, 1600);
      } catch (error) {
        window.alert(error instanceof TypeError
          ? 'Nu ne-am putut conecta la server.'
          : error.message);
      } finally {
        saveTheme.disabled = false;
      }
    });
    themeEditor.append(themeSelect, coverSelect, expirationInput, saveTheme);
    const linksPanel = document.createElement('section');
    linksPanel.className = 'album-links';
    const linksHeading = document.createElement('h4');
    linksHeading.textContent = 'Linkuri suplimentare';
    const linksHelp = document.createElement('p');
    linksHelp.textContent = 'Toate linkurile cer PIN-ul galeriei. Alege separat dacă fiecare permite încărcarea.';
    const linksList = document.createElement('div');
    linksList.className = 'album-links-list';
    const createLinkRow = document.createElement('div');
    createLinkRow.className = 'album-link-create';
    const createUploadLabel = document.createElement('label');
    const createUpload = document.createElement('input');
    createUpload.type = 'checkbox';
    createUploadLabel.append(createUpload, document.createTextNode(' Permite upload'));
    const createLink = document.createElement('button');
    createLink.className = 'album-theme-save';
    createLink.type = 'button';
    createLink.textContent = 'Creează link';
    createLink.addEventListener('click', async () => {
      createLink.disabled = true;
      try {
        const response = await api(`albums/${encodeURIComponent(album.slug)}/links`, {
          method: 'POST',
          body: JSON.stringify({ allow_upload: createUpload.checked }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Nu am putut crea linkul.');
        await refreshAlbums();
      } catch (error) {
        window.alert(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message);
      } finally {
        createLink.disabled = false;
      }
    });
    createLinkRow.append(createUploadLabel, createLink);
    (album.links || []).forEach((shareLink, index) => {
      const row = document.createElement('div');
      row.className = 'album-link-row';
      const anchor = document.createElement('a');
      anchor.href = `/g/${encodeURIComponent(album.slug)}?link=${encodeURIComponent(shareLink.id)}`;
      anchor.textContent = `${location.origin}${anchor.pathname}${anchor.search}`;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      anchor.title = `Link ${index + 1}`;
      const uploadLabel = document.createElement('label');
      const uploadToggle = document.createElement('input');
      uploadToggle.type = 'checkbox';
      uploadToggle.checked = shareLink.allow_upload;
      uploadLabel.append(uploadToggle, document.createTextNode(' Upload'));
      const saveLink = document.createElement('button');
      saveLink.className = 'album-theme-save';
      saveLink.type = 'button';
      saveLink.textContent = 'Salvează';
      saveLink.addEventListener('click', async () => {
        saveLink.disabled = true;
        try {
          const response = await api(`albums/${encodeURIComponent(album.slug)}/links/${encodeURIComponent(shareLink.id)}`, {
            method: 'PUT',
            body: JSON.stringify({ allow_upload: uploadToggle.checked }),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Nu am putut actualiza linkul.');
          await refreshAlbums();
        } catch (error) {
          window.alert(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message);
        } finally {
          saveLink.disabled = false;
        }
      });
      const removeLink = document.createElement('button');
      removeLink.className = 'album-delete';
      removeLink.type = 'button';
      removeLink.textContent = 'Revocă';
      removeLink.addEventListener('click', async () => {
        if (!window.confirm('Revoci acest link? Persoanele care îl folosesc nu îl vor mai putea utiliza.')) return;
        removeLink.disabled = true;
        try {
          const response = await api(`albums/${encodeURIComponent(album.slug)}/links/${encodeURIComponent(shareLink.id)}`, {
            method: 'DELETE',
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Nu am putut revoca linkul.');
          await refreshAlbums();
        } catch (error) {
          window.alert(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message);
          removeLink.disabled = false;
        }
      });
      row.append(anchor, uploadLabel, saveLink, removeLink);
      linksList.append(row);
    });
    linksPanel.append(linksHeading, linksHelp, createLinkRow, linksList);
    const remove = document.createElement('button');
    remove.className = 'album-delete';
    remove.type = 'button';
    remove.textContent = 'Șterge';
    remove.setAttribute('aria-label', `Șterge albumul ${album.title}`);
    remove.addEventListener('click', async () => {
      if (!window.confirm(`Ștergi albumul „${album.title}”? Linkul nu va mai funcționa. Fotografiile originale nu vor fi șterse.`)) return;
      remove.disabled = true;
      try {
        const response = await api(`albums/${encodeURIComponent(album.slug)}`, { method: 'DELETE' });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Nu am putut șterge albumul.');
        await refreshAlbums();
      } catch (error) {
        window.alert(error instanceof TypeError
          ? 'Nu ne-am putut conecta la server.'
          : error.message);
        remove.disabled = false;
      }
    });
    actions.append(link, themeEditor, linksPanel, remove);
    row.append(details, actions);
    list.append(row);
  });
}

async function loadAdmin() {
  try {
    const response = await api('session');
    if (!response.ok) throw new Error('Panoul de administrare nu este configurat.');
    const state = await response.json();
    loginSection.hidden = state.authenticated;
    dashboard.hidden = !state.authenticated;
    if (state.authenticated) {
      await refreshAlbums();
    } else {
      document.querySelectorAll('.album-pin-value').forEach(value => { value.textContent = ''; });
      document.querySelectorAll('.album-pin-control button').forEach(button => {
        if (!button.disabled) button.textContent = 'Afișează PIN-ul';
      });
    }
    if (state.authenticated) await refreshWorkerStatus();
  } catch (error) {
    loginSection.hidden = false;
    $('#adminLoginMessage').textContent = error.message;
  }
}

$('#adminLoginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#adminLoginButton');
  button.disabled = true;
  $('#adminLoginMessage').textContent = '';
  try {
    const response = await api('login', {
      method: 'POST',
      body: JSON.stringify({ password: $('#adminPassword').value }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Autentificarea a eșuat.');
    $('#adminPassword').value = '';
    await loadAdmin();
  } catch (error) {
    $('#adminLoginMessage').textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta la server.'
      : error.message;
  } finally {
    button.disabled = false;
  }
});

$('#createAlbumForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#createAlbumButton');
  button.disabled = true;
  $('#createAlbumMessage').textContent = '';
  try {
    const form = new FormData(event.currentTarget);
    const response = await api('albums', {
      method: 'POST',
      body: JSON.stringify({
        title: form.get('title'),
        slug: form.get('slug'),
        folder: form.get('folder'),
        theme: form.get('theme'),
        description: form.get('description'),
        expires_at: form.get('expires_at') ? new Date(form.get('expires_at')).toISOString() : null,
        pin: form.get('pin'),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Nu am putut crea galeria.');
    $('#createAlbumMessage').textContent = `Galeria a fost creată: ${location.origin}${result.url}`;
    event.currentTarget.reset();
    await refreshAlbums();
  } catch (error) {
    $('#createAlbumMessage').textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta la server.'
      : error.message;
  } finally {
    button.disabled = false;
  }
});

$('#adminLogout').addEventListener('click', async () => {
  await api('logout', { method: 'POST' });
  await loadAdmin();
});

loadAdmin();
window.setInterval(() => {
  if (!dashboard.hidden) refreshWorkerStatus();
}, 15000);
