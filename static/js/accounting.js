// Shared accounting UI for /apk and the retained /accounting route.
// Uses only the existing point fields, photo and file APIs. No technical requests.
const Accounting = (() => {
  let root, rows = [], total = 0, query = '', listSeq = 0;
  let dirty = false, saving = false, timer;
  const selected = new Set();
  let exporting = false;
  const pageSize = 50;
  const editable = () => ['accounting', 'admin'].includes(CURRENT_USER?.role) && can('accounting.edit');
  const message = (el, text) => { if (el) el.textContent = text; };

  function rowHtml(p) {
    return `<article data-brick="${esc(p.id)}" class="rounded-xl border border-slate-700/50 bg-slate-800/30 p-3 flex flex-col gap-2">
      <div class="flex items-start justify-between gap-2">
        <label class="shrink-0" title="Выбрать для выгрузки"><input type="checkbox" data-acc-select="${esc(p.id)}" aria-label="Выбрать АПК ${esc(p.id)}" ${selected.has(p.id) ? 'checked' : ''}></label>
        <div class="min-w-0 flex-1">
          <button type="button" data-acc-open="${esc(p.id)}" class="text-left text-sm font-semibold break-all">${esc(p.id)}</button>
          <span class="text-xs text-slate-400 break-all"> · инв. ${esc(p.accounting_inventory || '—')}</span>
          <div class="text-sm font-semibold break-words">${esc(p.accounting_name || 'Бухгалтерское название не указано')}</div>
          <div class="text-xs text-slate-400 break-words">${esc(p.address || 'Адрес не указан')}</div>
        </div>
        <button type="button" data-acc-open="${esc(p.id)}" class="${btnGhost} !py-1 !px-2 text-xs" title="Открыть карточку АПК">🗂</button>
      </div>
    </article>`;
  }

  function renderRows() {
    root.querySelector('[data-acc-list]').innerHTML = rows.map(rowHtml).join('') || '<p class="text-sm text-slate-400 py-6">АПК с бухгалтерскими сведениями не найдены. Для заполнения существующего АПК нажмите «+ АПК».</p>';
    message(root.querySelector('[data-acc-count]'), `(${total})`);
    root.querySelector('[data-acc-more]').classList.toggle('hidden', rows.length >= total);
    syncSelection();
  }

  function syncSelection() {
    const all = root.querySelector('[data-acc-select-all]');
    const count = rows.filter((p) => selected.has(p.id)).length;
    all.checked = rows.length > 0 && count === rows.length;
    all.indeterminate = count > 0 && count < rows.length;
    all.disabled = !rows.length;
    root.querySelectorAll('[data-acc-select]').forEach((input) => { input.checked = selected.has(input.dataset.accSelect); });
    message(root.querySelector('[data-acc-selected-count]'), `Выбрано: ${selected.size}`);
    root.querySelector('[data-acc-clear]').disabled = !selected.size;
    root.querySelector('[data-acc-export]').disabled = exporting || !selected.size;
    message(root.querySelector('[data-acc-export]'), exporting ? 'Готовим файл…' : 'Экспорт в Excel');
  }

  async function exportSelected() {
    if (exporting || !selected.size) return;
    const point_ids = [...selected];
    exporting = true; syncSelection();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetch('/api/accounting/export', {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ point_ids }),
      });
      if (response.status === 401) { window.location.href = '/login'; return; }
      if (!response.ok) {
        let reason = response.statusText;
        try { reason = (await response.json()).detail || reason; } catch { /* non-JSON error */ }
        throw new Error(typeof reason === 'string' ? reason : 'Не удалось сформировать файл. Проверьте выбранные комплексы.');
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url; link.download = 'accounting-apk.xlsx';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast(`Выгружено комплексов: ${response.headers.get('X-Points-Exported') || point_ids.length}`, 'ok');
    } catch (e) {
      toast(e.name === 'AbortError' ? 'Не удалось дождаться выгрузки. Попробуйте ещё раз.' : e.message, 'warn');
    } finally { clearTimeout(timeout); exporting = false; syncSelection(); }
  }

  async function load(append = false) {
    const seq = ++listSeq;
    const offset = append ? rows.length : 0;
    const more = root.querySelector('[data-acc-more]');
    more.disabled = true;
    try {
      const data = await Api.getPage(`/api/accounting/points?q=${encodeURIComponent(query)}&limit=${pageSize}&offset=${offset}`);
      if (seq !== listSeq) return;
      rows = append ? [...rows, ...data.items] : data.items;
      total = data.total;
      renderRows();
    } catch (e) {
      if (seq === listSeq) message(root.querySelector('[data-acc-count]'), e.message);
    } finally { if (seq === listSeq) more.disabled = false; }
  }

  async function mount(container) {
    root = container;
    root.classList.add('glass', 'rounded-3xl', 'shadow-xl');
    root.innerHTML = `<div class="flex flex-wrap items-center gap-2 mb-4">
        <span class="${btnPrimary}">📋 Справочник АПК</span>
      </div><div class="flex items-center justify-between mb-4 gap-2 flex-wrap">
        <h2 class="font-display text-lg font-bold">Справочник АПК <span data-acc-count class="text-sm font-normal text-slate-500" aria-live="polite"></span></h2>
        ${editable() ? '<button type="button" data-acc-add class="rounded-xl px-3.5 py-2 text-sm font-semibold bg-sky-500 hover:bg-sky-400 text-white shadow-lg shadow-sky-500/25 transition-all duration-300 hover:scale-[1.02]">+ АПК</button>' : ''}
      </div><div class="mb-3">
        <input id="accSearch" type="search" maxlength="200" autocomplete="off" class="w-full rounded-xl px-3.5 py-2.5 text-sm accent-ring transition-all duration-300" aria-label="Поиск по номеру АПК, бухгалтерскому названию или инвентарному номеру" placeholder="Поиск по номеру АПК, бухгалтерскому названию или инвентарному номеру">
      </div><div class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-700/40 bg-slate-800/20 p-2.5 mb-2">
        <label class="flex items-center gap-2 text-xs"><input type="checkbox" data-acc-select-all>Выбрать все загруженные</label>
        <span data-acc-selected-count class="text-xs text-slate-400" aria-live="polite" title="Включая выбранные комплексы из других результатов поиска">Выбрано: 0</span>
        <button type="button" data-acc-clear class="${btnGhost} !py-1 !px-3 text-xs" disabled>Снять выбор</button>
        <button type="button" data-acc-export class="${btnPrimary} !py-1 !px-3 text-xs" disabled>Экспорт в Excel</button>
      </div><div data-acc-list class="space-y-2"></div>
      <div class="text-center mt-4"><button type="button" data-acc-more class="${btnGhost} hidden">Показать ещё</button></div>`;
    root.querySelector('#accSearch').oninput = (e) => {
      query = e.target.value.trim(); ++listSeq; clearTimeout(timer);
      timer = setTimeout(() => load(), 250);
    };
    root.querySelector('[data-acc-more]').onclick = () => load(true);
    root.querySelector('[data-acc-add]')?.addEventListener('click', choose);
    root.querySelector('[data-acc-export]').onclick = exportSelected;
    root.querySelector('[data-acc-clear]').onclick = () => { selected.clear(); syncSelection(); };
    root.addEventListener('change', (e) => {
      if (e.target.matches('[data-acc-select-all]')) {
        const next = new Set(selected);
        rows.forEach((p) => e.target.checked ? next.add(p.id) : next.delete(p.id));
        if (next.size > 5000) toast('За один раз можно выгрузить до 5000 комплексов.', 'warn');
        else { selected.clear(); next.forEach((id) => selected.add(id)); }
      } else if (e.target.matches('[data-acc-select]')) {
        const id = e.target.dataset.accSelect;
        if (!e.target.checked) selected.delete(id);
        else if (selected.size < 5000) selected.add(id);
        else toast('За один раз можно выгрузить до 5000 комплексов.', 'warn');
      } else return;
      syncSelection();
    });
    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-acc-open]');
      if (b) open(b.dataset.accOpen);
    });
    await load();
  }

  function closeCard() {
    if (saving) { toast('Дождитесь завершения сохранения', 'warn'); return; }
    if (dirty && !confirm('Закрыть без добавления камеры?')) return;
    dirty = false; editing = false;
    $('#modal').innerHTML = '';
    load();
  }

  function open(id) {
    return openPointCard(id, { accounting: true, onChange: () => load() });
  }

  function choose() {
    dirty = false;
    modal(`<h2 class="font-display text-lg font-bold mb-4">Добавить камеру в бухгалтерский учёт</h2>
      <form id="accFind" class="space-y-3">
        <label class="${labelCls}" for="accName">Название камеры</label>
        <input id="accName" type="text" required maxlength="4000" class="${fieldCls} w-full" placeholder="Камера М-4, 1120 км">
        <label for="accInventory" class="${labelCls}">Инвентарный номер</label>
        <div class="flex gap-2"><input id="accInventory" type="text" required maxlength="200" autocomplete="off" class="${fieldCls} w-full">
          <button class="${btnGhost}" type="submit">Найти</button></div>
      </form>
      <p id="accFindState" class="text-sm my-3" aria-live="polite"></p><div id="accCandidates" class="space-y-2"></div>
      <button type="button" id="accCandidatesMore" class="${btnGhost} mt-3 hidden">Ещё варианты</button>
      <p id="accSelected" class="text-sm my-3 break-words" aria-live="polite"></p>
      <div class="flex gap-2 mt-4"><button type="button" id="accAddSave" class="${btnPrimary}" disabled>Добавить</button>
        <button type="button" class="cancel ${btnGhost}">Закрыть</button></div>`);
    let found = [], selected = null, inventory = '', searchSeq = 0;
    const area = $('#accCandidates'), state = $('#accFindState'), more = $('#accCandidatesMore');
    const nameInput = $('#accName'), invInput = $('#accInventory'), add = $('#accAddSave');
    const selection = $('#accSelected');
    const resetSelection = () => { selected = null; add.disabled = true; message(selection, ''); };
    const search = async (append = false) => {
      const local = ++searchSeq;
      resetSelection();
      const entered = invInput.value.trim();
      if (!entered) { message(state, 'Введите инвентарный номер'); return; }
      inventory = entered; more.disabled = true;
      message(state, 'Поиск…');
      try {
        const data = await Api.getPage(`/api/accounting/candidates?inventory=${encodeURIComponent(inventory)}&offset=${append ? found.length : 0}`);
        if (local !== searchSeq || !area.isConnected) return;
        found = append ? found.concat(data.items) : data.items;
        message(state, data.total ? `Найдено: ${data.total}. Выберите существующий АПК и проверьте номера.` : 'АПК не найден. Проверьте инвентарный номер.');
        area.innerHTML = found.map((p, i) => `<button type="button" data-candidate="${i}" class="w-full text-left rounded-xl border border-slate-700/50 bg-slate-800/30 p-3 break-words">
          <b class="break-all">${esc(p.id)}</b><br>${esc(p.address || 'Адрес не указан')}
          <small class="block break-all">Справочник: ${esc(p.inventory_number || '—')} · Бухгалтерия: ${esc(p.accounting_inventory || '—')}</small>
          ${p.has_accounting ? '<small class="block">Уже в бухгалтерском учёте — название и номер будут обновлены после подтверждения.</small>' : ''}</button>`).join('');
        more.classList.toggle('hidden', found.length >= data.total);
      } catch (e) { if (local === searchSeq) message(state, e.message); }
      finally { if (local === searchSeq) more.disabled = false; }
    };
    $('#accFind').onsubmit = (e) => { e.preventDefault(); search(); };
    nameInput.oninput = () => { dirty = true; };
    invInput.oninput = () => { dirty = true; ++searchSeq; resetSelection(); found = []; area.innerHTML = ''; more.classList.add('hidden'); message(state, ''); };
    more.onclick = () => search(true);
    area.onclick = async (e) => {
      const b = e.target.closest('[data-candidate]');
      if (!b || !found[+b.dataset.candidate] || saving) return;
      const local = ++searchSeq;
      resetSelection();
      try {
        const p = await Api.get(`/api/accounting/point?point_id=${encodeURIComponent(found[+b.dataset.candidate].id)}`);
        if (local !== searchSeq || !area.isConnected) return;
        selected = p; add.disabled = false;
        message(selection, `Выбран АПК ${p.id} · ${p.address || 'Адрес не указан'}`);
      } catch (e) { if (local === searchSeq) message(state, e.message); }
    };
    add.onclick = async () => {
      if (saving || !selected || !$('#accFind').reportValidity()) return;
      if (!nameInput.value.trim()) { nameInput.focus(); return; }
      const p = selected;
      const name = p.accounting_fields.find((f) => f.name.toLowerCase().replace(/ё/g, 'е') === 'наименование в бухгалтерском учете');
      const inv = p.accounting_fields.find((f) => f.name.toLowerCase() === 'инвентарный номер');
      if (!name || !inv) { message(state, 'Бухгалтерские поля недоступны. Обратитесь к администратору.'); return; }
      if (p.has_accounting && !confirm(`АПК ${p.id} уже в учёте. Обновить название и бухгалтерский инвентарный номер? Остальные поля сохранятся.`)) return;
      saving = true;
      const controls = [...$('#modal').querySelectorAll('input, button')];
      controls.forEach((el) => { el.disabled = true; });
      message(state, 'Сохраняю…');
      try {
        await Api.put(`/api/point-category-fields/point/${encodeURIComponent(p.id)}`, {
          values: { [name.id]: nameInput.value.trim(), [inv.id]: inventory }, expected_revision: p.accounting_revision,
        });
        dirty = false;
        $('#modal').innerHTML = ''; editing = false;
        // Show the newly added entry even if the previous search excluded it.
        query = ''; root.querySelector('#accSearch').value = '';
        await load(); toast('Камера добавлена в бухгалтерский учёт', 'ok');
      } catch (e) {
        message(state, `${e.message} Выберите АПК повторно, чтобы проверить актуальные данные.`);
        resetSelection();
      } finally {
        saving = false; controls.forEach((el) => { el.disabled = false; }); add.disabled = !selected;
      }
    };
    nameInput.focus();
  }

  return { mount, close: closeCard, open };
})();

if (document.getElementById('accountingPoints')) {
  // Legacy route stays functional and uses exactly the same forms and APIs.
  document.addEventListener('DOMContentLoaded', async () => {
    try {
      CURRENT_USER = await Api.get('/api/auth/me');
      initUserUi();
      $('#userMenuBtn').onclick = () => $('#userMenu').classList.toggle('hidden');
      document.addEventListener('click', (e) => { if (!e.target.closest('#userMenuBtn, #userMenu')) $('#userMenu').classList.add('hidden'); });
      $('#changePwBtn').onclick = () => { $('#userMenu').classList.add('hidden'); changePasswordForm({ forced: false }); };
      $('#logoutBtn').onclick = async () => { await Api.post('/api/auth/logout', {}); window.location.href = '/login'; };
      if (CURRENT_USER.must_change_password) { changePasswordForm({ forced: true }); return; }
      await Accounting.mount(document.querySelector('main'));
    } catch (e) { toast(e.message, 'warn'); }
  });
}
