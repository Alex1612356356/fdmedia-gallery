const $ = selector => document.querySelector(selector);
const loginSection = $('#adminLogin');
const dashboard = $('#adminDashboard');
const api = (path, options = {}) => fetch(`/api/admin/${path}`, {
  credentials: 'same-origin',
  ...options,
  headers: { 'Content-Type': 'application/json', ...options.headers },
});

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
    const details = document.createElement('div');
    const title = document.createElement('h3');
    title.textContent = album.title;
    const folder = document.createElement('p');
    const theme = document.createElement('p');
    const themeNames = {
      default: 'Implicită',
      wedding: 'Nuntă',
      'baptism-boy': 'Botez băiat',
      'baptism-girl': 'Botez fată',
      'prom-boy': 'Majorat băiat',
      'prom-girl': 'Majorat fată',
    };
    theme.textContent = `${themeNames[album.theme] || themeNames.default} · ${album.folder} · /g/${album.slug}`;
    details.append(title, theme);
    if (album.description) {
      const description = document.createElement('p');
      description.textContent = album.description;
      details.append(description);
    }
    const link = document.createElement('a');
    link.className = 'album-open';
    link.href = `/g/${encodeURIComponent(album.slug)}`;
    link.textContent = 'Deschide →';
    const actions = document.createElement('div');
    actions.className = 'album-actions';
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
    actions.append(link, remove);
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
    if (state.authenticated) await refreshAlbums();
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
