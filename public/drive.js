const $ = selector => document.querySelector(selector);
const login = $('#driveLogin');
const dashboard = $('#driveDashboard');
let currentFolder = '';
let items = [];

function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  return fetch(`/api/admin/${path}`, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
    headers,
  });
}

function setMessage(message, isError = false) {
  const element = $('#driveMessage');
  element.textContent = message;
  element.classList.toggle('drive-message-error', isError);
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toLocaleString('ro-RO', { maximumFractionDigits: 1 })} ${units[unit]}`;
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('ro-RO');
}

function renderBreadcrumbs() {
  const nav = $('#driveBreadcrumbs');
  nav.replaceChildren();
  const parts = currentFolder ? currentFolder.split('/') : [];
  const locations = [{ name: 'Drive-ul meu', path: '' }];
  parts.forEach((name, index) => locations.push({ name, path: parts.slice(0, index + 1).join('/') }));
  locations.forEach((location, index) => {
    if (index) {
      const separator = document.createElement('span');
      separator.className = 'drive-breadcrumb-separator';
      separator.textContent = '/';
      nav.append(separator);
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = location.name;
    button.disabled = index === locations.length - 1;
    button.addEventListener('click', () => openFolder(location.path));
    nav.append(button);
  });
}

function renderItems() {
  const body = $('#driveItems');
  const query = $('#driveSearch').value.trim().toLocaleLowerCase('ro-RO');
  const filtered = items.filter(item => item.name.toLocaleLowerCase('ro-RO').includes(query));
  body.replaceChildren();
  $('#driveEmpty').hidden = filtered.length !== 0;
  filtered.forEach(item => {
    const row = document.createElement('tr');
    const nameCell = document.createElement('td');
    const name = document.createElement('button');
    name.className = `drive-item-name${item.type === 'folder' ? ' drive-folder-name' : ''}`;
    name.type = 'button';
    name.textContent = `${item.type === 'folder' ? '▰' : '▤'}  ${item.name}`;
    name.title = item.name;
    if (item.type === 'folder') {
      name.addEventListener('click', () => openFolder(currentFolder ? `${currentFolder}/${item.name}` : item.name));
    } else {
      name.disabled = true;
    }
    nameCell.append(name);

    const dateCell = document.createElement('td');
    dateCell.textContent = formatDate(item.modified);
    const sizeCell = document.createElement('td');
    sizeCell.textContent = item.type === 'file' ? formatSize(item.size) : '—';
    const actionsCell = document.createElement('td');
    actionsCell.className = 'drive-row-actions';
    if (item.type === 'file') {
      const path = currentFolder ? `${currentFolder}/${item.name}` : item.name;
      const download = document.createElement('a');
      download.className = 'drive-row-action';
      download.href = `/api/admin/drive/download?path=${encodeURIComponent(path)}`;
      download.textContent = 'Descarcă';
      const remove = document.createElement('button');
      remove.className = 'drive-row-action drive-delete';
      remove.type = 'button';
      remove.textContent = 'Șterge';
      remove.addEventListener('click', () => deleteFile(path, item.name, remove));
      actionsCell.append(download, remove);
    }
    row.append(nameCell, dateCell, sizeCell, actionsCell);
    body.append(row);
  });
}

async function openFolder(path = currentFolder) {
  setMessage('');
  try {
    const response = await api(`drive?folder=${encodeURIComponent(path)}`);
    if (response.status === 401) {
      login.hidden = false;
      dashboard.hidden = true;
      return;
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Nu am putut deschide folderul.');
    currentFolder = result.folder;
    items = result.items;
    renderBreadcrumbs();
    renderItems();
  } catch (error) {
    setMessage(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message, true);
  }
}

async function loadDrive() {
  try {
    const response = await api('session');
    if (!response.ok) throw new Error('Panoul de administrare nu este configurat.');
    const state = await response.json();
    login.hidden = state.authenticated;
    dashboard.hidden = !state.authenticated;
    if (state.authenticated) await openFolder('');
  } catch (error) {
    login.hidden = false;
    $('#driveLoginMessage').textContent = error.message;
  }
}

async function createFolder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = await api('drive/folders', {
      method: 'POST',
      body: JSON.stringify({ folder: currentFolder, name: $('#newFolderName').value }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Nu am putut crea folderul.');
    form.reset();
    form.hidden = true;
    $('#newFolderToggle').hidden = false;
    await openFolder();
    setMessage('Folderul a fost creat.');
  } catch (error) {
    setMessage(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message, true);
  } finally {
    button.disabled = false;
  }
}

function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  form.append('folder', currentFolder);
  for (const file of files) form.append('files', file, file.name);
  const progress = $('#driveProgress');
  const progressBar = $('#driveProgressBar');
  const progressLabel = $('#driveProgressLabel');
  progress.hidden = false;
  progressBar.style.width = '0%';
  progressLabel.textContent = `Se pregătesc ${files.length} fișier(e)...`;
  setMessage('');

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/admin/drive/upload');
  xhr.withCredentials = true;
  xhr.upload.addEventListener('progress', event => {
    if (!event.lengthComputable) return;
    const percent = Math.round(event.loaded / event.total * 100);
    progressBar.style.width = `${percent}%`;
    progressLabel.textContent = `Se încarcă... ${percent}%`;
  });
  xhr.addEventListener('load', async () => {
    let result = {};
    try { result = JSON.parse(xhr.responseText); } catch { /* The server returned a non-JSON response. */ }
    progress.hidden = true;
    if (xhr.status === 401) {
      login.hidden = false;
      dashboard.hidden = true;
      return;
    }
    if (xhr.status < 200 || xhr.status >= 300) {
      setMessage(result.error || 'Nu am putut încărca fișierele.', true);
      return;
    }
    $('#fileInput').value = '';
    setMessage(`Au fost încărcate ${result.uploaded.length} fișier(e).`);
    await openFolder();
  });
  xhr.addEventListener('error', () => {
    progress.hidden = true;
    setMessage('Nu am putut încărca fișierele. Verifică conexiunea și spațiul disponibil pe server.', true);
  });
  xhr.addEventListener('abort', () => {
    progress.hidden = true;
    setMessage('Încărcarea a fost întreruptă.', true);
  });
  xhr.send(form);
}

async function deleteFile(path, name, button) {
  if (!window.confirm(`Ștergi definitiv fișierul „${name}”?`)) return;
  button.disabled = true;
  try {
    const response = await api('drive', { method: 'DELETE', body: JSON.stringify({ path }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Nu am putut șterge fișierul.');
    await openFolder();
    setMessage('Fișierul a fost șters.');
  } catch (error) {
    setMessage(error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message, true);
    button.disabled = false;
  }
}

$('#driveLoginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#driveLoginButton');
  button.disabled = true;
  $('#driveLoginMessage').textContent = '';
  try {
    const response = await api('login', {
      method: 'POST',
      body: JSON.stringify({ password: $('#drivePassword').value }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Autentificarea a eșuat.');
    $('#drivePassword').value = '';
    await loadDrive();
  } catch (error) {
    $('#driveLoginMessage').textContent = error instanceof TypeError ? 'Nu ne-am putut conecta la server.' : error.message;
  } finally {
    button.disabled = false;
  }
});

$('#driveLogout').addEventListener('click', async () => {
  await api('logout', { method: 'POST' });
  currentFolder = '';
  await loadDrive();
});
$('#driveSearch').addEventListener('input', renderItems);
$('#newFolderToggle').addEventListener('click', () => {
  $('#newFolderForm').hidden = false;
  $('#newFolderToggle').hidden = true;
  $('#newFolderName').focus();
});
$('#cancelNewFolder').addEventListener('click', () => {
  $('#newFolderForm').hidden = true;
  $('#newFolderToggle').hidden = false;
});
$('#newFolderForm').addEventListener('submit', createFolder);
$('#uploadButton').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', event => uploadFiles([...event.currentTarget.files]));
const dropzone = $('#driveDropzone');
dropzone.addEventListener('dragover', event => {
  event.preventDefault();
  dropzone.classList.add('drive-dropzone-active');
});
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drive-dropzone-active'));
dropzone.addEventListener('drop', event => {
  event.preventDefault();
  dropzone.classList.remove('drive-dropzone-active');
  uploadFiles([...event.dataTransfer.files]);
});
dropzone.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    $('#fileInput').click();
  }
});

loadDrive();
