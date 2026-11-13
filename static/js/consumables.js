// Партионный учёт расходников: остаток, выдача сотруднику, установка на АПК и возврат.
let consumableItems = [];
let consumablePeople = [];
let consumablePersonId = '';

const consumableActionLabels = {
  receipt: 'Поступление',
  issue: 'Выдача сотруднику',
  install: 'Установлено на АПК',
  return: 'Возврат на склад',
};

async function renderConsumablesTab() {
  $('#content').innerHTML = loadingHtml('Данные загружаются…', 'text-sm text-slate-500 text-center py-8');
  try {
    const [items, meta] = await Promise.all([
      Api.get('/api/consumables'),
      Api.get('/api/equipment/meta'),
    ]);
    consumableItems = items;
    consumablePeople = meta.people || [];
  } catch (e) {
    $('#content').innerHTML = `<p class="text-sm text-rose-400">Не удалось загрузить: ${esc(e.message)}</p>`;
    return;
  }
  consumablePersonId = consumablePeople.some((p) => String(p.id) === consumablePersonId) ? consumablePersonId : '';
  $('#content').innerHTML = `
    <div class="flex flex-wrap items-end justify-between gap-3 mb-4">
      <div><h2 class="font-display text-lg font-bold">Расходные материалы</h2>
        <p class="text-xs text-slate-400">Остаток склада, выдача сотрудникам, установка на АПК и возврат считаются отдельно.</p></div>
    </div>
    ${can('warehouse.edit') ? `<form id="consumableAdd" class="rounded-xl border border-slate-700/50 bg-slate-800/30 p-3 flex flex-wrap items-end gap-2 mb-4">
      <label class="${labelCls} flex-1 min-w-[160px]">Наименование<input name="name" required maxlength="120" class="${fieldCls}" placeholder="Например, болт М6"></label>
      <label class="${labelCls}">Единица<input name="unit" value="шт." required maxlength="24" class="${fieldCls} !w-20"></label>
      <label class="${labelCls}">Начальный остаток<input name="quantity" type="number" min="0" step="1" value="0" class="${fieldCls} !w-20"></label>
      <button class="${btnPrimary}">Добавить</button>
    </form>` : ''}
    <div id="consumablePeopleBox" class="rounded-xl border border-slate-700/50 bg-slate-800/30 p-3 mb-4">
      <div class="flex flex-wrap items-end gap-2">
        <label class="${labelCls} min-w-[220px]">Сотрудник
          <select id="consumablePerson" class="${fieldCls}">
            <option value="">Выберите сотрудника для выдачи или возврата</option>
            ${consumablePeople.map((p) => `<option value="${p.id}" ${String(p.id) === consumablePersonId ? 'selected' : ''}>${esc(p.full_name)}</option>`).join('')}
          </select>
        </label>
        <p class="text-xs text-slate-400">Остаток на руках расходуется только при записи расхода по выезду в боте.</p>
      </div>
      <div id="consumablePersonBalance" class="mt-3"></div>
    </div>
    <div class="space-y-2 mb-6" id="consumableList"></div>
    <details class="rounded-xl border border-slate-700/50 bg-slate-800/30 p-3">
      <summary class="cursor-pointer text-sm font-semibold">История движений</summary>
      <div id="consumableHistory" class="mt-3 text-xs text-slate-300">Открыть, чтобы загрузить историю.</div>
    </details>`;
  wireConsumables();
  paintConsumableItems();
  await loadConsumablePersonBalance();
}

function paintConsumableItems() {
  const box = $('#consumableList');
  if (!box) return;
  box.innerHTML = consumableItems.map((item) => `<div class="rounded-xl border border-slate-700/50 bg-slate-800/30 p-3">
    <div class="flex flex-wrap items-start justify-between gap-2 mb-2">
      <div class="min-w-[160px]"><b>${esc(item.name)}</b> <span class="text-xs text-slate-400">${esc(item.unit)}</span>
        ${item.active ? '' : '<span class="text-xs text-rose-300">выключен</span>'}
        <div class="text-xs text-slate-400 mt-1">На складе: <b class="text-slate-200">${item.stock_quantity}</b> · На руках у сотрудников: ${item.held_quantity}</div>
      </div>
      ${can('warehouse.edit') ? `<span class="flex gap-1">
        <button type="button" data-cons-edit="${item.id}" class="${btnGhost} !py-1 !px-2 text-xs">Изменить</button>
        <button type="button" data-cons-active="${item.id}" class="${btnGhost} !py-1 !px-2 text-xs">${item.active ? 'Выключить' : 'Включить'}</button>
      </span>` : ''}
    </div>
    ${can('warehouse.edit') && item.active ? `<div class="grid md:grid-cols-2 gap-2">
      <form data-cons-receive="${item.id}" class="flex flex-wrap items-end gap-1.5 rounded-lg bg-slate-900/20 p-2">
        <label class="${labelCls}">Поступило<input name="quantity" type="number" min="1" step="1" required class="${fieldCls} !py-1.5 !w-24"></label>
        <label class="${labelCls} flex-1 min-w-[100px]">Примечание<input name="note" class="${fieldCls} !py-1.5"></label>
        <button class="${btnGhost} !py-1.5 text-xs">Принять</button>
      </form>
      <form data-cons-issue="${item.id}" class="flex flex-wrap items-end gap-1.5 rounded-lg bg-slate-900/20 p-2">
        <label class="${labelCls}">Выдать<input name="quantity" type="number" min="1" max="${item.stock_quantity}" step="1" required class="${fieldCls} !py-1.5 !w-24"></label>
        <span class="text-xs text-slate-400 flex-1">Сотрудника выберите выше</span>
        <button class="${btnGhost} !py-1.5 text-xs" ${consumablePersonId ? '' : 'disabled'}>Выдать</button>
      </form>
    </div>` : ''}
  </div>`).join('') || '<p class="text-sm text-slate-500 text-center py-6">Расходных материалов пока нет</p>';
  box.querySelectorAll('[data-cons-receive]').forEach((form) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target.elements;
      await runConsumableAction(() => Api.post(`/api/consumables/${form.dataset.consReceive}/receive`, {
        quantity: +f.quantity.value, note: f.note.value.trim(),
      }));
    };
  });
  box.querySelectorAll('[data-cons-issue]').forEach((form) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (!consumablePersonId) { alert('Сначала выберите сотрудника'); return; }
      const f = e.target.elements;
      await runConsumableAction(() => Api.post('/api/consumables/issue', {
        consumable_id: +form.dataset.consIssue, quantity: +f.quantity.value,
        person_id: +consumablePersonId,
      }));
    };
  });
  box.querySelectorAll('[data-cons-edit]').forEach((button) => {
    button.onclick = async () => {
      const item = consumableItems.find((i) => i.id === +button.dataset.consEdit);
      if (!item) return;
      const name = prompt('Название расходного материала:', item.name);
      if (name === null) return;
      const unit = prompt('Единица измерения:', item.unit);
      if (unit === null) return;
      await runConsumableAction(() => Api.put(`/api/consumables/${item.id}`, {
        name: name.trim(), unit: unit.trim(), active: item.active,
      }));
    };
  });
  box.querySelectorAll('[data-cons-active]').forEach((button) => {
    button.onclick = async () => {
      const item = consumableItems.find((i) => i.id === +button.dataset.consActive);
      if (!item) return;
      const active = !item.active;
      if (!confirm(active ? `Включить «${item.name}»?` : `Выключить «${item.name}»?`)) return;
      await runConsumableAction(() => Api.put(`/api/consumables/${item.id}`, {
        name: item.name, unit: item.unit, active,
      }));
    };
  });
}

async function loadConsumablePersonBalance() {
  const box = $('#consumablePersonBalance');
  if (!box) return;
  if (!consumablePersonId) { box.innerHTML = ''; return; }
  let items;
  try { items = await Api.get(`/api/consumables/people/${consumablePersonId}`); }
  catch (e) { box.innerHTML = `<p class="text-sm text-rose-400">${esc(e.message)}</p>`; return; }
  box.innerHTML = items.length ? `<div class="space-y-1">${items.map((item) => `<form data-cons-return="${item.id}" class="flex flex-wrap items-end gap-2 border-t border-slate-700/40 pt-2">
    <span class="text-sm flex-1 min-w-[160px]">${esc(item.name)}: <b>${item.quantity} ${esc(item.unit)}</b> на руках</span>
    ${can('warehouse.edit') ? `<label class="${labelCls}">Вернуть<input name="quantity" type="number" min="1" max="${item.quantity}" step="1" required class="${fieldCls} !py-1.5 !w-24"></label>
    <button class="${btnGhost} !py-1.5 text-xs">Вернуть на склад</button>` : ''}
  </form>`).join('')}</div>` : '<p class="text-xs text-slate-500">У сотрудника нет неиспользованных материалов.</p>';
  box.querySelectorAll('[data-cons-return]').forEach((form) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target.elements;
      await runConsumableAction(() => Api.post('/api/consumables/return', {
        consumable_id: +form.dataset.consReturn, quantity: +f.quantity.value,
        person_id: +consumablePersonId,
      }));
    };
  });
}

async function runConsumableAction(action) {
  try {
    await action();
    await renderConsumablesTab();
  } catch (e) { alert('Не удалось выполнить операцию: ' + e.message); }
}

function wireConsumables() {
  const person = $('#consumablePerson');
  if (person) person.onchange = async () => {
    consumablePersonId = person.value;
    paintConsumableItems();
    await loadConsumablePersonBalance();
  };
  const add = $('#consumableAdd');
  if (add) add.onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target.elements;
    try {
      await Api.post('/api/consumables', {
        name: f.name.value.trim(), unit: f.unit.value.trim(), quantity: +f.quantity.value,
      });
      await renderConsumablesTab();
    } catch (err) { alert('Не удалось добавить: ' + err.message); }
  };
  const history = document.querySelector('#content details');
  if (history) history.ontoggle = async () => {
    if (!history.open) return;
    const box = $('#consumableHistory');
    box.textContent = 'Загружаю историю…';
    try {
      const rows = await Api.get('/api/consumables/history?limit=500');
      box.innerHTML = rows.length ? `<div class="space-y-1 max-h-80 overflow-auto">${rows.map((r) => `<div class="border-b border-slate-700/40 py-1.5">
        <b>${esc(r.name)}</b> · ${esc(consumableActionLabels[r.action] || r.action)}: ${r.quantity} ${esc(r.unit)}
        ${r.person_name ? ` · ${esc(r.person_name)}` : ''}${r.point_id ? ` · АПК ${esc(r.point_id)}` : ''}
        <span class="text-slate-500"> · ${esc(fmtDateTime(r.created_at, true))}</span>
        ${r.note ? `<div class="text-slate-400">${esc(r.note)}</div>` : ''}
      </div>`).join('')}</div>` : '<p class="text-slate-500">Движений пока нет</p>';
    } catch (e) { box.innerHTML = `<p class="text-rose-400">${esc(e.message)}</p>`; }
  };
}
