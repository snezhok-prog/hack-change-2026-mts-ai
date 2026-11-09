'use strict';
const el = (id) => document.getElementById(id);
let sessionId = null;
let busy = false;
let hasReply = false;
let leadSaved = false;
let archiveVersion = 0;

async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } }); }
  catch { throw new Error('Нет соединения с сервером. Проверьте подключение и повторите запрос.'); }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('Сервер вернул некорректный ответ. Попробуйте ещё раз.'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте заполнение полей.');
  return data;
}
function setBusy(value, action = 'message') {
  busy = value;
  document.querySelectorAll('.progress-steps li').forEach((step, index) => step.classList.toggle('active', index <= (leadSaved ? 2 : hasReply ? 1 : 0)));
  document.querySelector('.form-hint').hidden = hasReply;
  el('send').disabled = value || !sessionId;
  el('save-lead').disabled = value || !sessionId || !hasReply || leadSaved;
  el('new-chat').disabled = value;
  el('question').disabled = value || !sessionId;
  document.querySelectorAll('[data-question]').forEach(button => { button.disabled = value || !sessionId; });
  el('send').textContent = value && action === 'message' ? 'Ищем ответ…' : 'Отправить';
  el('save-lead').textContent = leadSaved ? 'Заявка сохранена' : value && action === 'lead' ? 'Сохраняем…' : 'Сохранить заявку';
  el('messages').setAttribute('aria-busy', String(value && action === 'message'));
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
  return item;
}
async function newSession(focusInput = true) {
  setBusy(true, 'session'); showError('');
  try {
    const data = await api('/api/sessions', { method: 'POST' });
    sessionId = data.id; hasReply = false; leadSaved = false; archiveVersion += 1;
    document.body.classList.remove('has-messages');
    el('messages').replaceChildren(); el('lead-form').reset(); el('lead-summary').hidden = true;
    el('lead-status').textContent = ''; el('question').value = '';
    el('lead-summary').textContent = '';
    for (const id of ['name', 'phone', 'company']) el(id).readOnly = false;
    el('test-data').disabled = false;
    el('debug').checked = false; document.body.classList.remove('show-debug');
    el('admin-token').value = ''; el('archive-list').replaceChildren();
    el('archive-status').textContent = ''; el('archive-detail').textContent = '';
    el('archive-detail').hidden = true;
    document.querySelector('.review-panel').open = false;
  } catch (error) { showError(error.message); }
  finally { setBusy(false); if (focusInput) el('question').focus(); }
}
el('new-chat').addEventListener('click', () => newSession());
el('debug').addEventListener('change', (event) => document.body.classList.toggle('show-debug', event.target.checked));
document.querySelectorAll('[data-question]').forEach((button) => button.addEventListener('click', () => { el('question').value = button.dataset.question; el('question').focus(); }));
el('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy || !sessionId) return;
  const text = el('question').value.trim(); if (!text) return;
  document.body.classList.add('has-messages');
  setBusy(true); showError(''); const sent = appendMessage('user', text); el('question').value = '';
  try { const data = await api(`/api/sessions/${sessionId}/messages`, { method: 'POST', body: JSON.stringify({ text }) }); appendMessage('assistant', data.answer, data); hasReply = true;
    el('connection').textContent = 'Ответ получен. Можно продолжить консультацию.'; }
  catch (error) {
    showError(error.message);
    el('question').value = text;
    const status = document.createElement('span'); status.className = 'message-status';
    status.textContent = 'Ответ не получен. Вопрос восстановлен в поле ввода.';
    sent.append(status);
  }
  finally { setBusy(false); el('question').focus(); }
});
el('question').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !busy) {
    event.preventDefault(); el('chat-form').requestSubmit();
  }
});
el('lead-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy || !sessionId) return;
  setBusy(true, 'lead'); showError('');
  try {
    const data = await api(`/api/sessions/${sessionId}/lead`, { method: 'POST', body: JSON.stringify({ name: el('name').value, phone: el('phone').value, company: el('company').value, test_data_confirmed: el('test-data').checked }) });
    leadSaved = true;
    for (const id of ['name', 'phone', 'company']) el(id).readOnly = true;
    el('test-data').disabled = true;
    el('lead-status').textContent = 'Заявка сохранена в прототипе. Отправки в МТС не было.';
    el('lead-summary').textContent = data.summary; el('lead-summary').hidden = false;
  } catch (error) { showError(error.message); }
  finally { setBusy(false); }
});
el('load-archive').addEventListener('click', async () => {
  const version = ++archiveVersion;
  el('archive-status').textContent = 'Загрузка…'; el('archive-list').replaceChildren();
  el('archive-detail').textContent = ''; el('archive-detail').hidden = true;
  try {
    const sessions = await api('/api/admin/sessions', { headers: { Authorization: 'Bearer ' + el('admin-token').value } });
    if (version !== archiveVersion) return;
    el('archive-status').textContent = `Последних диалогов: ${sessions.length}`;
    for (const session of sessions) {
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = `${new Date(session.created_at).toLocaleString('ru-RU')} · ${session.message_count} сообщений · ${session.has_lead ? 'с заявкой' : 'без заявки'}`;
      button.addEventListener('click', async () => {
        try { const detail = await api(`/api/sessions/${session.id}`); if (version !== archiveVersion) return; el('archive-detail').textContent = JSON.stringify(detail, null, 2); el('archive-detail').hidden = false; }
        catch (error) { if (version === archiveVersion) el('archive-status').textContent = error.message; }
      });
      el('archive-list').append(button);
    }
  } catch (error) { if (version === archiveVersion) el('archive-status').textContent = error.message; }
});
async function start() {
  await newSession(false);
  try {
    const health = await api('/api/health');
    if (health.knowledge_base_ready === false) {
      el('connection').textContent = 'База знаний пока недоступна. Обратитесь к организатору демо.';
    } else if (health.model_configured === false) {
      el('connection').textContent = 'Модель пока не подключена. Обратитесь к организатору демо.';
    } else if (health.ready) {
      const model = health.model ? ' Модель: ' + health.model + '.' : '';
      el('connection').textContent = (health.provider_verified
        ? 'Сервис уже получал ответы модели; можно начать разговор.'
        : 'Сервис настроен; доступность модели проверится при первом вопросе.') + model;
    } else {
      el('connection').textContent = 'Консультант пока недоступен. Проверьте настройку сервиса и попробуйте позже.';
    }
  } catch { el('connection').textContent = 'Нет соединения с сервером.'; }
}
start();

