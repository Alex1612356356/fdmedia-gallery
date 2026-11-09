import PhotoSwipeLightbox from '/vendor/photoswipe-lightbox.esm.min.js';
const slug = location.pathname.split('/')[2];
const $ = s => document.querySelector(s), grid = $('#grid');
let items = [], favs = new Set(), onlyFav = false, isAdmin = false, sections = [], activeSection = 'all';
let uploadAllowed = false;
const shareLink = new URLSearchParams(location.search).get('link');
let imageObserver;
const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
const qualityRank = { low: 0, medium: 1, high: 2 };
let adaptiveQuality = initialQuality();
let measuredDownlink = null;
let measuredSamples = 0;
const api = (p, o = {}) => {
  const endpoint = new URL(`/api/${encodeURIComponent(slug)}/${p}`, location.origin);
  if (shareLink) endpoint.searchParams.set('link', shareLink);
  return fetch(endpoint, { credentials: 'same-origin', cache: 'no-store', ...o });
};
const url = (k, n, m) => `/m/${slug}/${k}/${encodeURIComponent(n)}${m === undefined ? '' : `?v=${encodeURIComponent(m)}`}`;

function qualityForDownlink(mbps) {
  if (mbps < 0.7) return 'low';
  if (mbps < 2.5) return 'medium';
  return 'high';
}

function initialQuality() {
  if (connection?.saveData || ['slow-2g', '2g'].includes(connection?.effectiveType)) return 'low';
  if (connection?.effectiveType === '3g') return 'medium';
  if (Number.isFinite(connection?.downlink)) return qualityForDownlink(connection.downlink);
  return 'medium';
}

function imageRendition() {
  return { low: 't', medium: 'm', high: 'l' }[adaptiveQuality];
}

function videoRendition() {
  return { low: 'v360', medium: 'v', high: 'v1080' }[adaptiveQuality];
}

function updateMediaLinks() {
  grid.querySelectorAll('.ph-link').forEach(link => {
    const item = visible()[Number(link.dataset.i)];
    if (!item) return;
    link.href = url(item.type === 'video' ? videoRendition() : imageRendition(), item.n, item.m);
  });
}

function applyMeasuredDownlink(image) {
  const entries = performance.getEntriesByName(image.currentSrc || image.src);
  const entry = entries[entries.length - 1];
  if (!entry || entry.transferSize < 16 * 1024) return;
  const transferDuration = entry.responseEnd - entry.responseStart;
  if (transferDuration < 50) return;
  const sample = entry.transferSize * 8 / (transferDuration * 1000);
  measuredDownlink = measuredDownlink === null ? sample : measuredDownlink * 0.65 + sample * 0.35;
  measuredSamples += 1;
  if (measuredSamples < 2) return;
  const nextQuality = qualityForDownlink(measuredDownlink);
  if (nextQuality !== adaptiveQuality) {
    adaptiveQuality = nextQuality;
    updateMediaLinks();
  }
}

connection?.addEventListener?.('change', () => {
  if (measuredSamples < 2) {
    adaptiveQuality = initialQuality();
    updateMediaLinks();
  }
});

function updateUploadPanel() {
  const enabled = isAdmin || uploadAllowed;
  $('#galleryUpload').hidden = !enabled;
  $('#galleryUploadTitle').textContent = isAdmin
    ? 'Încarcă în galeria ta'
    : 'Trimite fotografii galeriei';
  $('#galleryUploadHelp').textContent = isAdmin
    ? 'Fișierele vor fi adăugate direct în această galerie.'
    : 'Trage fotografiile și videoclipurile aici sau selectează-le de pe dispozitiv.';
}

function uploadGalleryFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append('files', file, file.name);
  const progress = $('#galleryUploadProgress');
  const progressBar = $('#galleryUploadProgressBar');
  const progressLabel = $('#galleryUploadProgressLabel');
  const message = $('#galleryUploadMessage');
  progress.hidden = false;
  progressBar.style.width = '0%';
  progressLabel.textContent = `Se pregătesc ${files.length} fișier(e)...`;
  message.textContent = '';
  message.classList.remove('gallery-upload-error');

  const endpoint = isAdmin
    ? new URL(`/api/admin/albums/${encodeURIComponent(slug)}/upload`, location.origin)
    : new URL(`/api/${encodeURIComponent(slug)}/upload`, location.origin);
  if (!isAdmin && shareLink) endpoint.searchParams.set('link', shareLink);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', endpoint);
  xhr.withCredentials = true;
  xhr.upload.addEventListener('progress', event => {
    if (!event.lengthComputable) return;
    const percent = Math.round(event.loaded / event.total * 100);
    progressBar.style.width = `${percent}%`;
    progressLabel.textContent = `Se încarcă... ${percent}%`;
  });
  xhr.addEventListener('load', () => {
    let result = {};
    try { result = JSON.parse(xhr.responseText); } catch { /* The server returned a non-JSON response. */ }
    progress.hidden = true;
    if (xhr.status === 401) {
      $('#gate').hidden = false;
      $('#err').textContent = 'Sesiunea a expirat. Introduceți din nou PIN-ul galeriei.';
      return;
    }
    if (xhr.status < 200 || xhr.status >= 300) {
      message.textContent = result.error || 'Nu am putut încărca fișierele.';
      message.classList.add('gallery-upload-error');
      return;
    }
    $('#galleryFileInput').value = '';
    message.textContent = `Au fost încărcate ${result.uploaded.length} fișier(e). Vor apărea în galerie după procesare.`;
    window.setTimeout(refreshGallery, 5000);
  });
  xhr.addEventListener('error', () => {
    progress.hidden = true;
    message.textContent = 'Nu am putut încărca fișierele. Verificați conexiunea și spațiul disponibil pe server.';
    message.classList.add('gallery-upload-error');
  });
  xhr.addEventListener('abort', () => {
    progress.hidden = true;
    message.textContent = 'Încărcarea a fost întreruptă.';
    message.classList.add('gallery-upload-error');
  });
  xhr.send(form);
}

async function load() {
  try {
    const r = await api('items');
    if (r.status === 401) { $('#gate').hidden = false; $('#pin').focus(); return; }
    if (r.status === 410) {
      const expired = await r.json();
      throw new Error(expired.error || 'Galeria a expirat.');
    }
    if (!r.ok) throw new Error(r.status === 404 ? 'Galeria nu a fost găsită. Verificați linkul primit.' : 'Galeria nu este disponibilă momentan. Încercați din nou în câteva clipe.');
    const d = await r.json();
    items = Array.isArray(d.items) ? d.items : [];
    uploadAllowed = d.upload_enabled === true;
    favs = new Set(Array.isArray(d.favs) ? d.favs : []);
    sections = Array.isArray(d.sections) ? d.sections : [];
    activeSection = 'all';
    try {
      const adminResponse = await fetch('/api/admin/session', { credentials: 'same-origin', cache: 'no-store' });
      if (adminResponse.status === 503) {
        isAdmin = false;
      } else if (!adminResponse.ok) {
        throw new Error('Nu am putut verifica permisiunile administratorului.');
      } else {
        const adminState = await adminResponse.json();
        isAdmin = adminState.authenticated === true;
      }
    } catch {
      isAdmin = false;
      $('#galleryNotice').textContent = 'Nu am putut verifica permisiunile administratorului; comenzile de administrare sunt ascunse.';
    }
    updateUploadPanel();
    $('#gate').hidden = true; $('#hero').hidden = false;
    $('#title').textContent = d.title || 'Galerie foto';
    $('#description').textContent = d.description || 'Am păstrat aici momentele voastre. Bucurați-vă de ele.';
    $('#hero').dataset.theme = d.theme || 'default';
    document.documentElement.dataset.theme = d.theme || 'default';
    const cover = $('#heroCover');
    if (d.cover) {
      $('#coverImage').src = url('t', d.cover, d.cover_m);
      $('#coverImage').dataset.name = d.cover;
      $('#coverImage').alt = '';
      cover.hidden = false;
    } else {
      $('#coverImage').removeAttribute('src');
      $('#coverImage').dataset.name = '';
      cover.hidden = true;
    }
    document.title = (d.title || 'Galerie foto') + ' — fdmedia.studio';
    $('#dlAll').href = `/api/${encodeURIComponent(slug)}/download`;
    $('#dlFavorites').href = `/api/${encodeURIComponent(slug)}/download-favorites`;
    $('#dlAll').hidden = false;
    $('#filterFav').hidden = false;
    updateFavoriteDownload();
    render(); initLightbox();
  } catch (error) {
    $('#gate').hidden = false;
    $('#err').textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta. Verificați conexiunea și încercați din nou.'
      : error.message;
  }
}
$('#pinForm').onsubmit = async e => {
  e.preventDefault();
  const button = $('#loginButton'), message = $('#err');
  button.disabled = true;
  button.querySelector('span').textContent = 'Se verifică PIN-ul...';
  message.textContent = '';
  try {
    const r = await api('login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin: $('#pin').value }) });
    const result = await r.json();
    if (!r.ok) {
      message.textContent = result.error || 'Nu am putut valida codul. Încercați din nou.';
      $('#pin').select();
      return;
    }
    $('#pin').value = '';
    await load();
  } catch {
    message.textContent = 'Nu ne-am putut conecta. Verificați conexiunea și încercați din nou.';
  } finally {
    button.disabled = false;
    button.querySelector('span').textContent = 'Deschide galeria';
  }
};
$('#togglePin').onclick = e => {
  const input = $('#pin'), visible = input.type === 'password';
  input.type = visible ? 'text' : 'password';
  e.currentTarget.textContent = visible ? 'Ascunde' : 'Afișează';
  e.currentTarget.setAttribute('aria-label', visible ? 'Ascunde PIN-ul' : 'Afișează PIN-ul');
  e.currentTarget.setAttribute('aria-pressed', String(visible));
  input.focus();
};

const visible = () => items.filter(i =>
  (!onlyFav || favs.has(i.n)) &&
  (activeSection === 'all' || (i.section || (i.n.includes('/') ? i.n.split('/')[0] : '__root__')) === activeSection)
);
function updateFavoriteDownload() {
  $('#favoriteDownloadCount').textContent = favs.size;
  $('#dlFavorites').hidden = favs.size === 0;
}
function renderSections() {
  const nav = $('#sectionFilters');
  nav.replaceChildren();
  if (!sections.length) {
    nav.hidden = true;
    activeSection = 'all';
    return;
  }
  nav.hidden = false;
  const choices = [{ id: 'all', title: 'Toate' }, ...sections];
  choices.forEach(section => {
    const button = document.createElement('button');
    button.className = 'section-filter';
    button.type = 'button';
    button.textContent = section.title;
    button.setAttribute('aria-pressed', String(activeSection === section.id));
    button.addEventListener('click', () => {
      activeSection = section.id;
      render();
    });
    nav.append(button);
  });
}
function render() {
  const list = visible();
  $('#countNumber').textContent = list.length.toLocaleString('ro-RO');
  $('#countLabel').textContent = onlyFav
    ? 'FOTOGRAFII FAVORITE'
    : activeSection !== 'all'
      ? (sections.find(section => section.id === activeSection)?.title || 'SECȚIUNE').toLocaleUpperCase('ro-RO')
      : 'FOTOGRAFII & CLIPURI';
  $('#favCount').textContent = favs.size;
  updateFavoriteDownload();
  $('#filterFav').setAttribute('aria-pressed', String(onlyFav));
  renderSections();
  grid.innerHTML = '';
  imageObserver?.disconnect();
  imageObserver = new IntersectionObserver(es => es.forEach(e => {
    if (!e.isIntersecting) return;
    const img = e.target;
    img.onload = () => {
      img.classList.add('in');
      applyMeasuredDownlink(img);
    };
    img.src = img.dataset.src;
    imageObserver.unobserve(img);
  }), { rootMargin: '600px' });
  const empty = list.length === 0;
  $('#emptyState').hidden = !empty;
  if (empty) {
    $('#emptyTitle').textContent = onlyFav ? 'Favoritele tale apar aici' : 'Galeria se pregătește';
    $('#emptyMessage').textContent = onlyFav
      ? 'Apasă pe inimă la fotografiile care îți plac pentru a le găsi ușor aici.'
      : 'Fotografiile vor apărea aici în curând. Revino în câteva momente.';
  }
  list.forEach((it, i) => {
    const card = document.createElement('article');
    card.className = 'ph'; card.style.setProperty('--ar', (it.w / it.h).toFixed(4));
    const link = document.createElement('a');
    link.className = 'ph-link';
    link.dataset.i = i;
    link.href = url(it.type === 'video' ? videoRendition() : imageRendition(), it.n, it.m);
    link.dataset.pswpWidth = it.w; link.dataset.pswpHeight = it.h;
    const image = document.createElement('img');
    image.alt = it.type === 'video' ? `Previzualizare videoclip: ${it.n}` : `Fotografie: ${it.n}`;
    image.dataset.src = url('t', it.n, it.m);
    link.append(image);
    const heart = document.createElement('button');
    heart.className = `heart ${favs.has(it.n) ? 'on' : ''}`;
    heart.type = 'button';
    heart.setAttribute('aria-label', favs.has(it.n) ? 'Elimină de la favorite' : 'Adaugă la favorite');
    heart.setAttribute('aria-pressed', String(favs.has(it.n)));
    heart.textContent = '♥';
    heart.onclick = ev => { ev.preventDefault(); ev.stopPropagation(); toggle(it.n); };
    card.append(link, heart);
    if (isAdmin) {
      const remove = document.createElement('button');
      remove.className = 'photo-delete';
      remove.type = 'button';
      remove.textContent = 'Șterge';
      remove.setAttribute('aria-label', `Șterge definitiv ${it.n}`);
      remove.addEventListener('click', () => deletePhoto(it, remove));
      card.append(remove);
    }
    if (it.type === 'video' || it.raw) {
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = it.type === 'video' ? 'Video' : 'RAW';
      card.append(tag);
    }
    grid.append(card); imageObserver.observe(image);
  });
}
async function deletePhoto(item, button) {
  if (!window.confirm(`Ștergi definitiv „${item.n}” din folderul galeriei? Originalul nu va mai putea fi recuperat.`)) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/admin/albums/${encodeURIComponent(slug)}/items`, {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: item.n }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Nu am putut șterge fotografia.');
    items = items.filter(existing => existing.n !== item.n);
    favs.delete(item.n);
    $('#galleryNotice').textContent = `Fotografia „${item.n}” a fost ștearsă definitiv.`;
    render();
    initLightbox();
  } catch (error) {
    $('#galleryNotice').textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta la server; fotografia nu a fost ștearsă.'
      : error.message;
    button.disabled = false;
  }
}
async function toggle(n) {
  const wasFavorite = favs.has(n);
  const on = !favs.has(n); on ? favs.add(n) : favs.delete(n);
  try {
    const r = await api('fav', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: n, on }) });
    if (!r.ok) throw new Error('Nu am putut actualiza favoritele. Încercați din nou.');
    favs = new Set(await r.json());
    $('#galleryNotice').textContent = on ? 'Adăugat la favorite.' : 'Eliminat din favorite.';
  } catch (error) {
    if (wasFavorite) favs.add(n); else favs.delete(n);
    $('#galleryNotice').textContent = error instanceof TypeError
      ? 'Conexiunea a fost întreruptă. Favoritele nu au fost salvate.'
      : error.message;
  }
  if (onlyFav && !favs.has(n)) render();
  else render_hearts();
}
function render_hearts() {
  $('#favCount').textContent = favs.size;
  updateFavoriteDownload();
  const list = visible();
  $('#countNumber').textContent = list.length.toLocaleString('ro-RO');
  grid.querySelectorAll('.ph').forEach(card => {
    const heart = card.querySelector('.heart'), active = favs.has(list[card.querySelector('.ph-link').dataset.i]?.n);
    heart.classList.toggle('on', active);
    heart.setAttribute('aria-pressed', String(active));
    heart.setAttribute('aria-label', active ? 'Elimină de la favorite' : 'Adaugă la favorite');
  });
  lb?.pswp?.currSlide && lb.pswp.ui?.updateUIState?.();
  document.querySelector('.pswp__button--fav')?.classList.toggle('on', favs.has(lb?.pswp && visible()[lb.pswp.currIndex]?.n));
}

let lb;
function initLightbox() {
  lb?.destroy();
  lb = new PhotoSwipeLightbox({
    gallery: '#grid', children: 'a.ph-link', pswpModule: () => import('/vendor/photoswipe.esm.min.js'),
    secondaryZoomLevel: 1, maxZoomLevel: 3, bgOpacity: 1, wheelToZoom: true, preload: [1, 2],
  });
  const cur = () => visible()[lb.pswp.currIndex];
  lb.on('uiRegister', () => {
    lb.pswp.ui.registerElement({ name: 'fav', order: 8, isButton: true, html: '♥', title: 'Favorit',
      onClick: (e, el) => { toggle(cur().n); setTimeout(() => el.classList.toggle('on', favs.has(cur().n))); },
      onInit: (el, pswp) => pswp.on('change', () => el.classList.toggle('on', favs.has(cur().n))) });
    lb.pswp.ui.registerElement({ name: 'dl', order: 9, isButton: true, html: '⬇', title: 'Descarcă original',
      onClick: () => { const a = document.createElement('a'); a.href = url('o', cur().n, cur().m); a.download = cur().n; a.click(); } });
  });
  // Video slides
  lb.addFilter('itemData', d => { const it = visible()[+d.element?.dataset.i]; if (it?.type === 'video')
    return { ...d, type: 'video', videoSrc: url(videoRendition(), it.n, it.m), poster: url(imageRendition(), it.n, it.m) }; return d; });
  lb.on('contentLoad', e => { const { content } = e; if (content.data.type !== 'video') return; e.preventDefault();
    content.element = Object.assign(document.createElement('video'), { className: 'pswp__video', controls: true, playsInline: true, preload: 'metadata', poster: content.data.poster, src: content.data.videoSrc });
    content.onLoaded(); });
  lb.on('close', () => document.querySelectorAll('.pswp__video').forEach(v => v.pause()));
  lb.init();
}
async function refreshGallery() {
  if ($('#gate').hidden === false || document.querySelector('.pswp--open')) return;
  try {
    const response = await api('items');
    if (response.status === 410) {
      const expired = await response.json();
      $('#gate').hidden = false;
      $('#err').textContent = expired.error || 'Galeria a expirat.';
      return;
    }
    if (!response.ok) throw new Error('Nu am putut actualiza galeria.');
    const data = await response.json();
    const nextItems = Array.isArray(data.items) ? data.items : [];
    uploadAllowed = data.upload_enabled === true;
    updateUploadPanel();
    const fingerprint = list => JSON.stringify(list.map(item => [item.n, item.m, item.size]));
    const metadataChanged = (data.theme || 'default') !== document.documentElement.dataset.theme
      || (data.cover || null) !== $('#coverImage').dataset.name
      || JSON.stringify(data.sections || []) !== JSON.stringify(sections);
    if (fingerprint(items) === fingerprint(nextItems) && !metadataChanged) return;
    const added = nextItems.filter(item => !items.some(existing => existing.n === item.n)).length;
    items = nextItems;
    sections = Array.isArray(data.sections) ? data.sections : [];
    document.documentElement.dataset.theme = data.theme || 'default';
    $('#hero').dataset.theme = data.theme || 'default';
    $('#coverImage').dataset.name = data.cover || '';
    if (data.cover) {
      $('#coverImage').src = url('t', data.cover, data.cover_m);
      $('#heroCover').hidden = false;
    } else {
      $('#coverImage').removeAttribute('src');
      $('#heroCover').hidden = true;
    }
    render();
    initLightbox();
    $('#galleryNotice').textContent = added
      ? `${added} fotografii sau clipuri noi au fost adăugate.`
      : 'Galeria a fost actualizată.';
  } catch (error) {
    $('#galleryNotice').textContent = error instanceof TypeError
      ? 'Nu am putut actualiza galeria. Verificați conexiunea.'
      : error.message;
  }
}
window.setInterval(refreshGallery, 30000);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshGallery();
});
$('#filterFav').onclick = () => { onlyFav = !onlyFav; $('#galleryNotice').textContent = ''; render(); };
$('#galleryUploadButton').addEventListener('click', () => $('#galleryFileInput').click());
$('#galleryFileInput').addEventListener('change', event => uploadGalleryFiles([...event.currentTarget.files]));
const galleryDropzone = $('#galleryDropzone');
galleryDropzone.addEventListener('dragover', event => {
  event.preventDefault();
  galleryDropzone.classList.add('gallery-dropzone-active');
});
galleryDropzone.addEventListener('dragleave', () => galleryDropzone.classList.remove('gallery-dropzone-active'));
galleryDropzone.addEventListener('drop', event => {
  event.preventDefault();
  galleryDropzone.classList.remove('gallery-dropzone-active');
  uploadGalleryFiles([...event.dataTransfer.files]);
});
galleryDropzone.addEventListener('click', () => $('#galleryFileInput').click());
galleryDropzone.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    $('#galleryFileInput').click();
  }
});
load();
