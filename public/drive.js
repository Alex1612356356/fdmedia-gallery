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
  $('#driveCurrentTitle').textContent = parts.length ? parts[parts.length - 1] : 'Bun venit în FD Drive';
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

function createFolderCard(item) {
  const card = document.createElement('button');
  card.className = 'drive-folder-card';
  card.type = 'button';
  card.title = item.name;
  const icon = document.createElement('span');
  icon.className = 'drive-folder-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '▰';
  const name = document.createElement('span');
  name.className = 'drive-folder-title';
  name.textContent = item.name;
  const arrow = document.createElement('span');
  arrow.className = 'drive-folder-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  arrow.textContent = '›';
  card.append(icon, name, arrow);
  card.addEventListener('click', () => openFolder(currentFolder ? `${currentFolder}/${item.name}` : item.name));
  return card;
}

function fileType(name) {
  const extension = name.split('.').pop().toLocaleLowerCase('ro-RO');
  if (['jpg', 'jpeg', 'png', 'webp', 'avif', 'gif'].includes(extension)) return { label: extension.toUpperCase(), kind: 'image', preview: true };
  if (['mp4', 'mov', 'mkv', 'm4v', 'avi'].includes(extension)) return { label: 'VIDEO', kind: 'video', preview: false };
  if (extension === 'pdf') return { label: 'PDF', kind: 'pdf', preview: false };
  if (['doc', 'docx', 'odt', 'rtf', 'txt'].includes(extension)) return { label: 'DOC', kind: 'document', preview: false };
  if (['zip', '7z', 'rar', 'tar', 'gz'].includes(extension)) return { label: 'ZIP', kind: 'archive', preview: false };
  return { label: extension && extension !== name ? extension.slice(0, 5).toUpperCase() : 'FILE', kind: 'generic', preview: false };
}

function createFileCard(item) {
  const card = document.createElement('article');
  card.className = 'drive-file-card';
  const heading = document.createElement('div');
  heading.className = 'drive-file-heading';
  const type = fileType(item.name);
  const icon = document.createElement('span');
  icon.className = `drive-file-icon drive-file-icon-${type.kind}`;
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = type.label;
  const name = document.createElement('span');
  name.className = 'drive-file-name';
  name.textContent = item.name;
  name.title = item.name;

  const path = currentFolder ? `${currentFolder}/${item.name}` : item.name;
  const menu = document.createElement('details');
  menu.className = 'drive-file-menu';
  const menuToggle = document.createElement('summary');
  menuToggle.textContent = '⋮';
  menuToggle.setAttribute('aria-label', `Acțiuni pentru ${item.name}`);
  const menuItems = document.createElement('div');
  menuItems.className = 'drive-file-menu-items';
  const download = document.createElement('a');
  download.href = `/api/admin/drive/download?path=${encodeURIComponent(path)}`;
  download.textContent = 'Descarcă';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'drive-delete';
  remove.textContent = 'Șterge';
  remove.addEventListener('click', () => deleteFile(path, item.name, remove));
  menuItems.append(download, remove);
  menu.append(menuToggle, menuItems);
  heading.append(icon, name, menu);

  const preview = document.createElement('a');
  preview.className = `drive-file-preview drive-file-preview-${type.kind}`;
  preview.href = download.href;
  preview.setAttribute('aria-label', `Descarcă ${item.name}`);
  if (type.preview) {
    const image = document.createElement('img');
    image.src = `/api/admin/drive/preview?path=${encodeURIComponent(path)}`;
    image.alt = '';
    image.loading = 'lazy';
    preview.append(image);
  } else {
    const previewIcon = document.createElement('span');
    previewIcon.className = `drive-preview-symbol drive-preview-symbol-${type.kind}`;
    previewIcon.setAttribute('aria-hidden', 'true');
    previewIcon.textContent = type.label;
    preview.append(previewIcon);
  }

  const metadata = document.createElement('div');
  metadata.className = 'drive-file-meta';
  const modified = document.createElement('span');
  modified.textContent = `Modificat ${formatDate(item.modified)}`;
  const size = document.createElement('span');
  size.textContent = formatSize(item.size);
  metadata.append(modified, size);
  card.append(heading, preview, metadata);
  return card;
}

function renderItems() {
  const folderGrid = $('#driveFolders');
  const fileGrid = $('#driveFiles');
  const query = $('#driveSearch').value.trim().toLocaleLowerCase('ro-RO');
  const selectedType = $('#driveTypeFilter').value;
  const filtered = items.filter(item => {
    const matchesSearch = item.name.toLocaleLowerCase('ro-RO').includes(query);
    const matchesType = selectedType === 'all' || item.type === selectedType;
    return matchesSearch && matchesType;
  });
  const folders = filtered.filter(item => item.type === 'folder');
  const files = filtered.filter(item => item.type === 'file');
  folderGrid.replaceChildren(...folders.map(createFolderCard));
  fileGrid.replaceChildren(...files.map(createFileCard));
  $('#driveFolderSection').hidden = folders.length === 0;
  $('#driveFileSection').hidden = files.length === 0;
  $('#driveEmpty').hidden = filtered.length !== 0;
  $('#driveItemCount').textContent = `${filtered.length} ${filtered.length === 1 ? 'element' : 'elemente'}`;
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
$('#driveTypeFilter').addEventListener('change', renderItems);
$('#driveHome').addEventListener('click', () => openFolder(''));
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
$('#driveUploadShortcut').addEventListener('click', () => $('#fileInput').click());
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
  if (event.target !== dropzone) return;
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    $('#fileInput').click();
  }
});

loadDrive();
