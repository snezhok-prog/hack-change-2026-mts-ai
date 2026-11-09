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

## Архив для платформы

```sh
python scripts/build_submission.py
```

Результат — `dist/submission.zip`, в консоли SHA256. Внутри только `solution.py`, `consultant.py`, `model_client.py`, `retrieval.py`, `requirements.txt` в корне. Данные, ключи, веб-интерфейс и история диалогов не входят. `--output /path/submission.zip` меняет назначение.

Тест `tests/test_packaging.py` распаковывает архив в отдельную папку и запускает его отдельным процессом против локального синтетического HTTP API. Проверяются запросы, сохранение ID, UTF-8 и CSV-экранирование. Это проверка запуска и формата, а не оценка LLM-судьёй.

## Docker для локального веб-демо

Dockerfile и Compose проверены в GitHub Actions на Linux: сборка, запуск HTTP и сохранение SQLite после перезапуска. Локальная установка Docker на Windows ещё выполняется.

1. Создайте каталог `data` и положите предоставленный организаторами `knowledge-base.zip` внутрь.
2. Скопируйте `.env.example` в `.env`, заполните доступ к модели и собственный ключ архива `ADMIN_TOKEN`.
3. Выполните `docker compose up --build` и откройте http://127.0.0.1:8000.

Порт опубликован только на localhost. База монтируется read-only; история сохраняется в Docker volume `session-data`. `docker compose down` сохраняет историю; опцию `-v` не используйте, если она нужна. Сервис запускается от отдельного пользователя; один Uvicorn worker. Healthcheck проверяет доступность веб-сервера, а не качество/доступность модели.

## Работа команды

Руководитель проекта — Рома (Kunnjut); роли, зоны ответственности и порядок работы — в [AGENTS.md](AGENTS.md), он главнее остальных документов.

Прочитайте [архитектуру](docs/ARCHITECTURE.md), [план ИИ-ядра](docs/AI-CORE.md) и [статус/ограничения](docs/STATUS.md).

Для SSH: `git@github.com:snezhok-prog/hack-change-2026-mts-ai.git`.

Задачи первой итерации: Рома — ИИ-ядро и руководство ([#1](https://github.com/snezhok-prog/hack-change-2026-mts-ai/issues/1) переназначен), [Олег / UI #2](https://github.com/snezhok-prog/hack-change-2026-mts-ai/issues/2), [Глеб / демо #3](https://github.com/snezhok-prog/hack-change-2026-mts-ai/issues/3).

Код попадает в `main` только через PR, слияние — за Ромой. GitHub Actions автоматически проверяет тесты, упаковку ZIP и контейнер при push в main/feature-ветки и при PR в main. API модели и база организаторов для CI не нужны; тесты используют синтетические данные.
