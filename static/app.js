'use strict';
const el = (id) => document.getElementById(id);
let sessionId = null;
let busy = false;

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте заполнение полей.');
  return data;
}
function setBusy(value) {
  busy = value;
  for (const id of ['send', 'save-lead']) el(id).disabled = value || !sessionId;
  el('new-chat').disabled = value;
  el('send').textContent = value ? 'Ищем ответ…' : 'Отправить';
}
function showError(message) { el('error').textContent = message; el('error').hidden = !message; }
function appendMessage(role, text, metadata = {}) {
  const item = document.createElement('article'); item.className = 'message ' + role;
  const label = document.createElement('span'); label.className = 'author'; label.textContent = role === 'user' ? 'Вы' : 'Консультант';
  item.append(label, document.createTextNode(text));
  if (metadata.sources) {
    const detail = document.createElement('details'); detail.className = 'debug';
    const summary = document.createElement('summary'); summary.textContent = `Время: ${metadata.elapsed_ms} мс · Источников: ${metadata.sources.length}`;
    detail.append(summary);
    for (const source of metadata.sources) {
      const title = document.createElement('strong'); title.textContent = `[${source.id}] ${source.document} — ${source.heading}`;
      const content = document.createElement('pre'); content.textContent = source.text;
      detail.append(title, content);
    }
    item.append(detail);
  }
  el('messages').append(item); item.scrollIntoView({ behavior: 'auto', block: 'nearest' });
}
async function newSession() {
  setBusy(true); showError('');
  try {
    const data = await api('/api/sessions', { method: 'POST' });
    sessionId = data.id;
    el('messages').replaceChildren(); el('lead-form').reset(); el('lead-summary').hidden = true;
    el('lead-status').textContent = ''; el('question').value = '';
  } catch (error) { showError(error.message); }
  finally { setBusy(false); }
}
el('new-chat').addEventListener('click', newSession);
el('debug').addEventListener('change', (event) => document.body.classList.toggle('show-debug', event.target.checked));
document.querySelectorAll('[data-question]').forEach((button) => button.addEventListener('click', () => { el('question').value = button.dataset.question; el('question').focus(); }));
el('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy || !sessionId) return;
  const text = el('question').value.trim(); if (!text) return;
  setBusy(true); showError(''); appendMessage('user', text); el('question').value = '';
  try { const data = await api(`/api/sessions/${sessionId}/messages`, { method: 'POST', body: JSON.stringify({ text }) }); appendMessage('assistant', data.answer, data); }
  catch (error) { showError(error.message); }
  finally { setBusy(false); el('question').focus(); }
});
el('lead-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy || !sessionId) return;
  setBusy(true); showError('');
  try {
    const data = await api(`/api/sessions/${sessionId}/lead`, { method: 'POST', body: JSON.stringify({ name: el('name').value, phone: el('phone').value, company: el('company').value, test_data_confirmed: el('test-data').checked }) });
    el('lead-status').textContent = 'Заявка сохранена в прототипе. Отправки в МТС не было.';
    el('lead-summary').textContent = data.summary; el('lead-summary').hidden = false;
  } catch (error) { showError(error.message); }
  finally { setBusy(false); }
});
el('load-archive').addEventListener('click', async () => {
  el('archive-status').textContent = 'Загрузка…'; el('archive-list').replaceChildren();
  el('archive-detail').textContent = ''; el('archive-detail').hidden = true;
  try {
    const sessions = await api('/api/admin/sessions', { headers: { Authorization: 'Bearer ' + el('admin-token').value } });
    el('archive-status').textContent = `Последних диалогов: ${sessions.length}`;
    for (const session of sessions) {
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = `${new Date(session.created_at).toLocaleString('ru-RU')} · ${session.message_count} сообщений · ${session.has_lead ? 'с заявкой' : 'без заявки'}`;
      button.addEventListener('click', async () => {
        try { const detail = await api(`/api/sessions/${session.id}`); el('archive-detail').textContent = JSON.stringify(detail, null, 2); el('archive-detail').hidden = false; }
        catch (error) { el('archive-status').textContent = error.message; }
      });
      el('archive-list').append(button);
    }
  } catch (error) { el('archive-status').textContent = error.message; }
});
async function start() {
  await newSession();
  try { const health = await api('/api/health'); el('connection').textContent = health.ready ? 'Консультант настроен. Можно начать разговор.' : 'Демо-интерфейс запущен. Для ответов нужно подключить API модели и базу знаний.'; }
  catch { el('connection').textContent = 'Нет соединения с сервером.'; }
}
start();
