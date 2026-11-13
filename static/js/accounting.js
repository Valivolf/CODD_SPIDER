const ACCOUNTING_PAGE_SIZE = 50;
let CURRENT_USER = null;
let accountingUser = null;
let accountingRows = [];
let accountingTotal = 0;
let accountingBusy = false;
let accountingPendingSearch = false;
let accountingTimer = 0;

function accountingFieldHtml(field) {
  const label = `<label class="block text-xs text-slate-400 mb-1" for="acc-field-${field.id}">${esc(field.name)}</label>`;
  const value = field.value || '';
  const readonly = can('accounting.edit') ? '' : ' readonly';
  if (field.field_type === 'textarea') {
    return `<div>${label}<textarea id="acc-field-${field.id}" data-accounting-field="${field.id}" maxlength="4000"${readonly} class="w-full rounded-xl px-3 py-2 text-sm accent-ring transition-all duration-300">${esc(value)}</textarea></div>`;
  }
  const type = field.field_type === 'date' ? 'date' : field.field_type === 'number' || field.field_type === 'currency' ? 'number' : 'text';
  const step = type === 'number' ? ' step="any"' : '';
  return `<div>${label}<input id="acc-field-${field.id}" data-accounting-field="${field.id}" type="${type}"${step} maxlength="4000" value="${esc(value)}"${readonly} class="w-full rounded-xl px-3 py-2 text-sm accent-ring transition-all duration-300"></div>`;
}

function accountingCardHtml(point) {
  const coords = [point.lat, point.lon].filter(Boolean).join(', ');
  const status = point.stock_partial ? 'Частично на складе' : point.status || 'Не указан';
  const fields = point.accounting_fields || [];
  return `<article class="glass rounded-3xl p-4 md:p-6 shadow-xl" data-accounting-card="${esc(point.id)}">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-[220px] flex-1">
        <h2 class="font-display text-lg font-bold break-all">${esc(point.id)}</h2>
        <p class="text-sm text-slate-300 mt-1">${esc(point.address || 'Адрес не указан')}</p>
        <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400 mt-2">
          <span>Инвентарный №: ${esc(point.inventory_number || '—')}</span>
          <span>Статус: ${esc(status)}</span>
          <span>Координаты: ${esc(coords || '—')}</span>
        </div>
      </div>
      <div class="flex flex-wrap gap-2">
        <button type="button" data-point-photos="${esc(point.id)}" class="rounded-xl px-3 py-2 text-sm font-medium bg-slate-800/70 hover:bg-slate-700/70 border border-slate-700">Фото</button>
        <a href="/warehouse?point=${encodeURIComponent(point.id)}" class="rounded-xl px-3 py-2 text-sm font-medium bg-slate-800/70 hover:bg-slate-700/70 border border-slate-700">Оборудование</a>
        <button type="button" data-accounting-files="${esc(point.id)}" class="rounded-xl px-3 py-2 text-sm font-medium bg-slate-800/70 hover:bg-slate-700/70 border border-slate-700">Файлы</button>
      </div>
    </div>
    <form data-accounting-form="${esc(point.id)}" class="mt-4 border-t border-slate-700/50 pt-4">
      ${fields.length ? `<div class="grid grid-cols-1 md:grid-cols-2 gap-3">${fields.map(accountingFieldHtml).join('')}</div>` : '<p class="text-sm text-slate-400">Бухгалтерские поля не заполнены.</p>'}
      ${can('accounting.edit') && fields.length ? `<div class="flex items-center gap-3 mt-4"><button type="submit" class="rounded-xl px-4 py-2 text-sm font-medium bg-sky-500 hover:bg-sky-400 text-white">Сохранить поля</button><span data-accounting-save-state class="text-xs text-slate-400" aria-live="polite"></span></div>` : ''}
    </form>
    <section data-accounting-file-area="${esc(point.id)}" class="hidden mt-4 border-t border-slate-700/50 pt-4">
      <div data-accounting-file-list class="space-y-2 text-sm"></div>
      ${can('accounting.edit') ? `<form data-accounting-upload="${esc(point.id)}" class="mt-3 flex flex-wrap items-center gap-2">
        <input type="file" name="files" multiple class="w-full text-xs text-slate-400">
        <button type="submit" class="rounded-xl px-3 py-2 text-sm font-medium bg-sky-500 hover:bg-sky-400 text-white">Загрузить файлы</button>
        <span data-accounting-upload-state class="text-xs text-slate-400" aria-live="polite"></span>
      </form>` : ''}
    </section>
  </article>`;
}

function renderAccountingRows() {
  const area = $('#accountingPoints');
  if (!accountingRows.length) {
    area.innerHTML = '<p class="glass rounded-2xl p-4 text-sm text-slate-400">Комплексы не найдены.</p>';
  } else {
    area.innerHTML = accountingRows.map(accountingCardHtml).join('');
  }
  $('#accountingCount').textContent = `Найдено: ${accountingTotal}`;
  $('#accountingMore').classList.toggle('hidden', accountingRows.length >= accountingTotal);
}

async function loadAccountingPoints(append = false) {
  if (accountingBusy) {
    if (!append) accountingPendingSearch = true;
    return;
  }
  accountingBusy = true;
  const button = $('#accountingMore');
  button.disabled = true;
  if (!append) {
    $('#accountingPoints').innerHTML = '<p class="glass rounded-2xl p-4 text-sm text-slate-400">Данные загружаются…</p>';
    $('#accountingCount').textContent = 'Загрузка данных…';
  }
  try {
    const q = $('#accountingSearch').value.trim();
    const offset = append ? accountingRows.length : 0;
    const result = await Api.getPage(`/api/accounting/points?q=${encodeURIComponent(q)}&limit=${ACCOUNTING_PAGE_SIZE}&offset=${offset}`);
    accountingRows = append ? [...accountingRows, ...result.items] : result.items;
    accountingTotal = result.total;
    renderAccountingRows();
  } catch (error) {
    if (!append) $('#accountingPoints').innerHTML = `<p class="glass rounded-2xl p-4 text-sm text-rose-300">${esc(error.message)}</p>`;
    else toast(`Не удалось загрузить комплексы: ${error.message}`, 'warn');
    $('#accountingCount').textContent = 'Не удалось загрузить данные.';
  } finally {
    accountingBusy = false;
    button.disabled = false;
    if (accountingPendingSearch) {
      accountingPendingSearch = false;
      await loadAccountingPoints();
    }
  }
}

async function loadAccountingFiles(pointId, area) {
  const list = area.querySelector('[data-accounting-file-list]');
  list.innerHTML = '<p class="text-xs text-slate-400">Загружаю список файлов…</p>';
  try {
    const files = await Api.get(`/api/point-files?point_id=${encodeURIComponent(pointId)}`);
    list.innerHTML = files.length ? files.map((file) => `<div class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-700/50 p-2.5">
      <span class="min-w-0 flex-1 break-all">${esc(file.name)}${file.comment ? `<small class="block text-xs text-slate-400">${esc(file.comment)}</small>` : ''}</span>
      <a href="/api/point-files/${file.id}/file?download=1" class="text-sky-300">Скачать</a>
    </div>`).join('') : '<p class="text-xs text-slate-400">Файлов пока нет.</p>';
  } catch (error) {
    delete area.dataset.loaded;
    list.innerHTML = `<p class="text-xs text-rose-300">${esc(error.message)}</p>`;
  }
}

async function bootAccountingPage() {
  try {
    accountingUser = await Api.get('/api/auth/me');
  } catch (error) {
    $('#accountingPoints').innerHTML = `<p class="glass rounded-2xl p-4 text-sm text-rose-300">${esc(error.message)}</p>`;
    $('#accountingCount').textContent = 'Не удалось загрузить данные.';
    return;
  }
  CURRENT_USER = accountingUser;
  if (!can('accounting.view')) {
    window.location.href = '/';
    return;
  }
  initUserUi();
  if (accountingUser.must_change_password) changePasswordForm({ forced: true });

  $('#accountingSearch').addEventListener('input', () => {
    clearTimeout(accountingTimer);
    accountingTimer = setTimeout(() => loadAccountingPoints(), 250);
  });
  $('#accountingSearchClear').onclick = () => {
    clearTimeout(accountingTimer);
    $('#accountingSearch').value = '';
    loadAccountingPoints();
  };
  $('#accountingMore').onclick = () => loadAccountingPoints(true);

  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('[data-accounting-form]');
    if (form) {
      event.preventDefault();
      if (!can('accounting.edit')) return;
      const state = form.querySelector('[data-accounting-save-state]');
      const values = {};
      form.querySelectorAll('[data-accounting-field]').forEach((input) => { values[input.dataset.accountingField] = input.value; });
      state.textContent = 'Сохраняю…';
      try {
        await Api.put(`/api/point-category-fields/point/${encodeURIComponent(form.dataset.accountingForm)}`, { values });
        state.textContent = 'Сохранено';
      } catch (error) {
        state.textContent = error.message;
      }
      return;
    }

    const uploadForm = event.target.closest('[data-accounting-upload]');
    if (!uploadForm) return;
    event.preventDefault();
    const input = uploadForm.querySelector('input[type="file"]');
    const state = uploadForm.querySelector('[data-accounting-upload-state]');
    const area = uploadForm.closest('[data-accounting-file-area]');
    if (!input.files.length) {
      state.textContent = 'Выберите файлы.';
      return;
    }
    uploadForm.querySelector('button').disabled = true;
    try {
      for (let index = 0; index < input.files.length; index += 1) {
        const file = input.files[index];
        const data = new FormData();
        data.append('point_id', uploadForm.dataset.accountingUpload);
        data.append('file', file);
        state.textContent = `Загружаю ${index + 1} из ${input.files.length}…`;
        await Api.uploadWithProgress('/api/point-files', data);
      }
      input.value = '';
      state.textContent = 'Файлы загружены.';
      await loadAccountingFiles(uploadForm.dataset.accountingUpload, area);
    } catch (error) {
      state.textContent = error.message;
    } finally {
      uploadForm.querySelector('button').disabled = false;
    }
  });

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-accounting-files]');
    if (!button) return;
    const card = button.closest('[data-accounting-card]');
    const area = card.querySelector('[data-accounting-file-area]');
    const opening = area.classList.contains('hidden');
    area.classList.toggle('hidden', !opening);
    if (opening && !area.dataset.loaded) {
      area.dataset.loaded = '1';
      await loadAccountingFiles(button.dataset.accountingFiles, area);
    }
  });
  await loadAccountingPoints();
}

bootAccountingPage();
