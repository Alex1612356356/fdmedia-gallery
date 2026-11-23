import PhotoSwipeLightbox from '/vendor/photoswipe-lightbox.esm.min.js';
const shareToken = location.pathname.startsWith('/s/') ? location.pathname.split('/')[2] : null;
const isSharedAlbum = Boolean(shareToken);
const slug = isSharedAlbum ? null : location.pathname.split('/')[2];
const $ = s => document.querySelector(s), grid = $('#grid');
let items = [], favs = new Set(), onlyFav = false, isAdmin = false, sections = [], activeSection = 'all';
let sortOrder = 'date-asc', creatingShare = false;
const shareSelections = new Set();
const shareLink = new URLSearchParams(location.search).get('link');
let imageObserver;
const PHOTO_RENDITION = 'l';
const VIDEO_RENDITION = 'v';
const api = (p, o = {}) => {
  const endpoint = new URL(
    isSharedAlbum
      ? `/api/s/${encodeURIComponent(shareToken)}/${p}`
      : `/api/${encodeURIComponent(slug)}/${p}`,
    location.origin,
  );
  if (shareLink) endpoint.searchParams.set('link', shareLink);
  return fetch(endpoint, { credentials: 'same-origin', cache: 'no-store', ...o });
};
const url = (k, n, m, version) => {
  const cacheKey = [m, version].filter(value => value !== undefined && value !== null).join('-');
  const path = isSharedAlbum
    ? `/sm/${encodeURIComponent(shareToken)}/${k}/${encodeURIComponent(n)}`
    : `/m/${slug}/${k}/${encodeURIComponent(n)}`;
  return `${path}${cacheKey ? `?v=${encodeURIComponent(cacheKey)}` : ''}`;
};
const renditionVersion = item => item.type === 'video' ? item.video_version : item.photo_version;

async function load() {
  try {
    const r = await api('items');
    if (r.status === 401 && !isSharedAlbum) { $('#gate').hidden = false; $('#pin').focus(); return; }
    if (r.status === 410) {
      const expired = await r.json();
      throw new Error(expired.error || 'Galeria a expirat.');
    }
    if (!r.ok) throw new Error(r.status === 404 ? 'Galeria nu a fost găsită. Verificați linkul primit.' : 'Galeria nu este disponibilă momentan. Încercați din nou în câteva clipe.');
    const d = await r.json();
    items = Array.isArray(d.items) ? d.items : [];
    favs = new Set(Array.isArray(d.favs) ? d.favs : []);
    sections = Array.isArray(d.sections) ? d.sections : [];
    activeSection = 'all';
    isAdmin = false;
    if (!isSharedAlbum) {
      try {
        const adminResponse = await fetch('/api/admin/session', { credentials: 'same-origin', cache: 'no-store' });
        if (adminResponse.status !== 503 && !adminResponse.ok) {
          throw new Error('Nu am putut verifica permisiunile administratorului.');
        }
        if (adminResponse.ok) {
          const adminState = await adminResponse.json();
          isAdmin = adminState.authenticated === true;
        }
      } catch {
        $('#galleryNotice').textContent = 'Nu am putut verifica permisiunile administratorului; comenzile de administrare sunt ascunse.';
      }
    }
    $('#gate').hidden = true; $('#hero').hidden = false;
    $('#galleryTools').hidden = isSharedAlbum;
    $('#title').textContent = d.title || 'Galerie foto';
    $('#description').textContent = d.description || 'Am păstrat aici momentele voastre. Bucurați-vă de ele.';
    $('#hero').dataset.theme = d.theme || 'default';
    document.documentElement.dataset.theme = d.theme || 'default';
    const cover = $('#heroCover');
    if (d.cover) {
      $('#coverImage').src = url('t', d.cover, d.cover_m, d.cover_version);
      $('#coverImage').dataset.name = d.cover;
      $('#coverImage').alt = '';
      cover.hidden = false;
    } else {
      $('#coverImage').removeAttribute('src');
      $('#coverImage').dataset.name = '';
      cover.hidden = true;
    }
    document.title = (d.title || 'Galerie foto') + ' — fdmedia.studio';
    $('#dlAll').href = isSharedAlbum
      ? `/api/s/${encodeURIComponent(shareToken)}/download`
      : `/api/${encodeURIComponent(slug)}/download`;
    $('#dlAll').hidden = false;
    $('#filterFav').hidden = isSharedAlbum;
    if (!isSharedAlbum) {
      $('#dlFavorites').href = `/api/${encodeURIComponent(slug)}/download-favorites`;
      await refreshSharedAlbums();
    }
    updateFavoriteDownload();
    updateShareControls();
    render(); initLightbox();
  } catch (error) {
    if (isSharedAlbum) {
      $('#galleryNotice').textContent = error instanceof TypeError
        ? 'Nu ne-am putut conecta. Verificați conexiunea și încercați din nou.'
        : error.message;
    } else {
      $('#gate').hidden = false;
      $('#err').textContent = error instanceof TypeError
        ? 'Nu ne-am putut conecta. Verificați conexiunea și încercați din nou.'
        : error.message;
    }
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

const visible = () => {
  const list = items.filter(i =>
    (!onlyFav || favs.has(i.n)) &&
    (activeSection === 'all' || (i.section || (i.n.includes('/') ? i.n.split('/')[0] : '__root__')) === activeSection)
  );
  const compareName = (left, right) => left.n.localeCompare(right.n, 'ro', { numeric: true, sensitivity: 'base' });
  const date = item => Number(item.date) || (Number(item.m) / 1e6) || 0;
  list.sort((left, right) => {
    if (sortOrder === 'name-asc') return compareName(left, right);
    if (sortOrder === 'name-desc') return compareName(right, left);
    const difference = date(left) - date(right);
    return (sortOrder === 'date-asc' ? difference : -difference) || compareName(left, right);
  });
  return list;
};
function updateFavoriteDownload() {
  $('#favoriteDownloadCount').textContent = favs.size;
  $('#dlFavorites').hidden = isSharedAlbum || favs.size === 0;
}
function updateShareControls() {
  if (isSharedAlbum) return;
  $('#shareSelectionCount').textContent = `${shareSelections.size} selectate`;
  $('#createShareAlbum').disabled = creatingShare || shareSelections.size === 0;
  const allSelected = items.length > 0 && items.every(item => shareSelections.has(item.n));
  const selectAll = $('#selectAllSharePhotos');
  selectAll.disabled = creatingShare || items.length === 0;
  selectAll.textContent = allSelected ? 'Deselectează tot' : 'Selectează tot';
  selectAll.setAttribute('aria-pressed', String(allSelected));
}
function toggleShareSelection(item, button) {
  if (shareSelections.has(item.n)) shareSelections.delete(item.n);
  else shareSelections.add(item.n);
  const selected = shareSelections.has(item.n);
  button.classList.toggle('share-photo-selected', selected);
  button.setAttribute('aria-pressed', String(selected));
  button.setAttribute('aria-label', `${selected ? 'Elimină din selecție' : 'Adaugă la selecție'}: ${item.n}`);
  button.textContent = selected ? 'Selectată' : 'Selectează';
  updateShareControls();
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
    img.onload = () => img.classList.add('in');
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
    link.href = url(
      it.type === 'video' ? VIDEO_RENDITION : PHOTO_RENDITION,
      it.n,
      it.m,
      renditionVersion(it),
    );
    link.dataset.pswpWidth = it.w; link.dataset.pswpHeight = it.h;
    const image = document.createElement('img');
    image.alt = it.type === 'video' ? `Previzualizare videoclip: ${it.n}` : `Fotografie: ${it.n}`;
    image.loading = 'lazy';
    image.decoding = 'async';
    if (i < 8) image.fetchPriority = 'high';
    image.dataset.src = url('t', it.n, it.m, renditionVersion(it));
    link.append(image);
    card.append(link);
    if (!isSharedAlbum) {
      const select = document.createElement('button');
      select.className = `share-photo-select${shareSelections.has(it.n) ? ' share-photo-selected' : ''}`;
      select.type = 'button';
      select.textContent = shareSelections.has(it.n) ? 'Selectată' : 'Selectează';
      select.setAttribute('aria-pressed', String(shareSelections.has(it.n)));
      select.setAttribute('aria-label', `${shareSelections.has(it.n) ? 'Elimină din selecție' : 'Adaugă la selecție'}: ${it.n}`);
      select.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        toggleShareSelection(it, select);
      });
      card.append(select);
      const heart = document.createElement('button');
      heart.className = `heart ${favs.has(it.n) ? 'on' : ''}`;
      heart.type = 'button';
      heart.setAttribute('aria-label', favs.has(it.n) ? 'Elimină de la favorite' : 'Adaugă la favorite');
      heart.setAttribute('aria-pressed', String(favs.has(it.n)));
      heart.textContent = '♥';
      heart.onclick = ev => { ev.preventDefault(); ev.stopPropagation(); toggle(it.n); };
      card.append(heart);
    }
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
    shareSelections.delete(item.n);
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
    if (!isSharedAlbum) {
      lb.pswp.ui.registerElement({ name: 'fav', order: 8, isButton: true, html: '♥', title: 'Favorit',
        onClick: (e, el) => { toggle(cur().n); setTimeout(() => el.classList.toggle('on', favs.has(cur().n))); },
        onInit: (el, pswp) => pswp.on('change', () => el.classList.toggle('on', favs.has(cur().n))) });
    }
    lb.pswp.ui.registerElement({ name: 'dl', order: 9, isButton: true, html: '⬇', title: 'Descarcă original',
      onClick: () => { const a = document.createElement('a'); a.href = url('o', cur().n, cur().m); a.download = cur().n; a.click(); } });
  });
  // Video slides
  lb.addFilter('itemData', d => { const it = visible()[+d.element?.dataset.i]; if (it?.type === 'video')
    return { ...d, type: 'video', videoSrc: url(VIDEO_RENDITION, it.n, it.m, renditionVersion(it)), poster: url(PHOTO_RENDITION, it.n, it.m, renditionVersion(it)) }; return d; });
  lb.on('contentLoad', e => { const { content } = e; if (content.data.type !== 'video') return; e.preventDefault();
    content.element = Object.assign(document.createElement('video'), { className: 'pswp__video', controls: true, playsInline: true, preload: 'metadata', poster: content.data.poster, src: content.data.videoSrc });
    content.onLoaded(); });
  lb.on('close', () => document.querySelectorAll('.pswp__video').forEach(v => v.pause()));
  lb.init();
}
async function refreshSharedAlbums() {
  const list = $('#sharedAlbumList');
  list.replaceChildren();
  try {
    const response = await fetch(`/api/${encodeURIComponent(slug)}/shares`, {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('Nu am putut încărca albumele create.');
    const shares = await response.json();
    shares.forEach(share => {
      const row = document.createElement('div');
      row.className = 'shared-album-row';
      const link = document.createElement('a');
      link.href = `/s/${encodeURIComponent(share.token)}`;
      link.textContent = share.title;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const copy = document.createElement('button');
      copy.className = 'share-revoke';
      copy.type = 'button';
      copy.textContent = 'Copiază linkul';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(link.href);
          $('#shareAlbumMessage').textContent = `Linkul „${share.title}” a fost copiat.`;
        } catch {
          $('#shareAlbumMessage').textContent = `Copiază linkul din bara browserului: ${link.href}`;
        }
      });
      const revoke = document.createElement('button');
      revoke.className = 'share-revoke';
      revoke.type = 'button';
      revoke.textContent = 'Revocă';
      revoke.addEventListener('click', async () => {
        if (!window.confirm(`Revoci linkul pentru „${share.title}”?`)) return;
        revoke.disabled = true;
        try {
          const result = await fetch(`/api/${encodeURIComponent(slug)}/shares/${encodeURIComponent(share.token)}`, {
            method: 'DELETE',
            credentials: 'same-origin',
          });
          if (!result.ok) throw new Error('Nu am putut revoca linkul.');
          await refreshSharedAlbums();
          $('#shareAlbumMessage').textContent = 'Linkul albumului a fost revocat.';
        } catch (error) {
          $('#shareAlbumMessage').textContent = error instanceof TypeError
            ? 'Nu ne-am putut conecta la server.'
            : error.message;
          revoke.disabled = false;
        }
      });
      row.append(link, copy, revoke);
      list.append(row);
    });
  } catch (error) {
    $('#shareAlbumMessage').textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta la server pentru a încărca albumele create.'
      : error.message;
  }
}
$('#createShareAlbum').addEventListener('click', async () => {
  const title = $('#shareAlbumTitle').value.trim();
  if (!title) {
    $('#shareAlbumMessage').textContent = 'Scrie un nume pentru albumul nou.';
    $('#shareAlbumTitle').focus();
    return;
  }
  creatingShare = true;
  updateShareControls();
  $('#shareAlbumMessage').textContent = 'Se creează albumul...';
  try {
    const response = await fetch(`/api/${encodeURIComponent(slug)}/shares`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, items: [...shareSelections] }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Nu am putut crea albumul.');
    shareSelections.clear();
    $('#shareAlbumTitle').value = '';
    $('#shareAlbumMessage').textContent = `Albumul a fost creat: ${location.origin}/s/${result.share.token}`;
    await refreshSharedAlbums();
    render();
  } catch (error) {
    $('#shareAlbumMessage').textContent = error instanceof TypeError
      ? 'Nu ne-am putut conecta la server.'
      : error.message;
  } finally {
    creatingShare = false;
    updateShareControls();
  }
});
$('#selectAllSharePhotos').addEventListener('click', () => {
  const allSelected = items.length > 0 && items.every(item => shareSelections.has(item.n));
  if (allSelected) {
    shareSelections.clear();
  } else {
    items.forEach(item => shareSelections.add(item.n));
  }
  updateShareControls();
  render();
});
$('#sortOrder').addEventListener('change', event => {
  sortOrder = event.currentTarget.value;
  render();
  initLightbox();
});
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
    const fingerprint = list => JSON.stringify(list.map(item => [
      item.n, item.m, item.size, item.photo_version, item.video_version,
    ]));
    const metadataChanged = (data.theme || 'default') !== document.documentElement.dataset.theme
      || (data.cover || null) !== $('#coverImage').dataset.name
      || JSON.stringify(data.sections || []) !== JSON.stringify(sections);
    if (fingerprint(items) === fingerprint(nextItems) && !metadataChanged) return;
    const added = nextItems.filter(item => !items.some(existing => existing.n === item.n)).length;
    items = nextItems;
    const availableNames = new Set(items.map(item => item.n));
    shareSelections.forEach(name => {
      if (!availableNames.has(name)) shareSelections.delete(name);
    });
    sections = Array.isArray(data.sections) ? data.sections : [];
    document.documentElement.dataset.theme = data.theme || 'default';
    $('#hero').dataset.theme = data.theme || 'default';
    $('#coverImage').dataset.name = data.cover || '';
    if (data.cover) {
      $('#coverImage').src = url('t', data.cover, data.cover_m, data.cover_version);
      $('#heroCover').hidden = false;
    } else {
      $('#coverImage').removeAttribute('src');
      $('#heroCover').hidden = true;
    }
    render();
    updateShareControls();
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
$('#filterFav').onclick = () => { onlyFav = !onlyFav; $('#galleryNotice').textContent = ''; render(); };
if (isSharedAlbum) $('#gate').hidden = true;
load();
