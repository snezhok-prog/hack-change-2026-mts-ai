# Hack&Change 2026 — EnergyTool AI consultant

Начальный проверяемый прототип: retrieval по Markdown, консультация через API модели, пакетный CSV и локальный веб-чат с историей/заявками. Код публичный; исходные материалы организаторов и контакты в git не входят.

## Запуск

Python 3.11+. Из корня репозитория:

```sh
python -m venv .venv
# Активируйте окружение своей оболочки.
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m uvicorn web:create_app --factory --host 127.0.0.1 --port 8000
```

Откройте http://127.0.0.1:8000. Без настройки модели интерфейс честно сообщает о недоступности консультации. Реальные ответы требуют предоставленного API и `data/knowledge-base.zip` (локально, не коммитить).

Переменные окружения: `LLM_BASE_URL` (база API, обычно заканчивается `/v1`), `LLM_API_KEY`, `LLM_MODEL`; поддерживаются алиасы `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` / `GEN_MODEL`. Идентификатор модели берётся из текущего доступа организаторов. `.env` автоматически не загружается.

`DATA_DIR` — каталог входных данных; `STATE_DIR` — каталог SQLite (по умолчанию `work/state`); `ADMIN_TOKEN` — локальный ключ просмотра архива. В веб-прототипе используйте выдуманные контакты. Заявки сохраняются локально, отправки в CRM/МТС нет.

## Пакетный запуск

```sh
python -m pip install -r requirements.txt
python solution.py
```

В `DATA_DIR` нужны `questions.csv` с колонками `id,question` и `knowledge-base.zip`. Результат — `submission.csv` в рабочем каталоге. Код выхода: 0 — все вызовы успешны; 2 — есть ошибки модели (строки отмечены недоступностью); 1 — ошибка конфигурации/входа. Модельные ошибки не считаются успешной консультацией.

## Работа команды

Прочитайте [протокол Codex ↔ Claude](docs/COLLABORATION.md), [архитектуру](docs/ARCHITECTURE.md) и [статус/ограничения](docs/STATUS.md).

Для SSH: `git@github.com:snezhok-prog/hack-change-2026-mts-ai.git`. Почтовые ветки `coordination/*` не являются ветками кода и не сливаются в main.
