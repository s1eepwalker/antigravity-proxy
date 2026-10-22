# agy-claude Implementation Plan

> Исторический план: выполнен с последующими уточнениями. Реальный backend Gemini 3.8 Flash — `gemini-3.8-flash-tiered` с medium thinking, модель по умолчанию — Gemini 3.8 Flash. Актуальная документация — README.md. Чекбоксы ниже отражают первоначальные шаги, а не незавершённые задачи.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Рабочий алиас `agy-claude`, отправляющий запросы Claude Code в локальный Antigravity Proxy на свежие модели.

**Architecture:** Дополнительный Anthropic `/v1/messages`-адаптер переводит запросы в уже существующий Gemini envelope через существующую сборку OpenAI-запроса и преобразует Gemini-ответы в Anthropic JSON/SSE; текущие маршруты сохраняются. Локальная конфигурация Claude Code изолирована в новом `~/.claude/agy-settings.json`.

**Tech Stack:** Python 3.12 stdlib (`unittest`, `http.server`), JSON, Bash, Claude Code 2.1.305.

**Spec:** `docs/superpowers/specs/2026-10-22-agy-claude-design.md`

## Global Constraints

- Без новых зависимостей; текущий `/v1/chat/completions` не ломать.
- Новые значения моделей: `gemini-4-argon-medium`, `gemini-3.8-flash-medium`, `claude-opus-5-5-medium`, `claude-sonnet-5-5-medium` — кандидаты из CLI, не подтверждены успешным внешним вызовом.
- Отдельный settings-файл и новый алиас; существующие настройки и алиасы не менять. Default — `claude-opus-5-5`.
- Только `127.0.0.1:8877`; без постоянного автозапуска и без обращений к Google без отдельного разрешения.
- Без коммитов и push до явного запроса пользователя.

## Review Focus

- Два вызова одного tool в одном ответе: ID и индексы блоков остаются разными (Task 2, 3).
- Assistant tool calls и user tool results в одной истории: имена и `thoughtSignature` сохраняются (Task 2).
- Неподдерживаемый input block/некорректные аргументы: HTTP 400, не тихая текстовая заглушка (Task 2, 3).
- Upstream падает до начала и после начала SSE: клиент получает явную ошибку, не `message_stop` успешного ответа (Task 3).
- Финальный Gemini chunk с usage без candidate: `message_delta` передаёт токены, без потери прошлых блоков (Task 3).

---

### Task 1: Модели и обратная совместимость

**Files:**
- Modify: `antigravity_proxy.py:86-102`
- Modify: `README.md`, `AGENTS.md`
- Create: `tests/__init__.py`, `tests/test_models.py`

**Interfaces:**
- Produces: `MODEL_MAP: dict[str, tuple[str, str | None]]`, включая четыре новые friendly aliases при сохранении старых.

- [ ] Написать `test_current_model_map_and_legacy_aliases`: проверить `MODEL_MAP["claude-opus-5-5"] == ("claude-opus-5-5-medium", None)`, аналогично остальные три пары из spec, сохранить `MODEL_MAP["claude-opus-4.6"]`; при отсутствии `model` использовать `gemini-3.8-flash`.
- [ ] Запустить `python3 -m unittest tests.test_models -v`; ожидается FAIL на отсутствующих моделях.
- [ ] Добавить новые записи в `MODEL_MAP`, выставить разумную актуальную `DEFAULT_MODEL`, обновить таблицы моделей и исправить ложный совет о прямом `ANTHROPIC_BASE_URL` до появления Anthropic-маршрута; GPT-OSS указать как не подтверждённый backend ID.
- [ ] Повторить `python3 -m unittest tests.test_models -v`; ожидается PASS.

### Task 2: Конвертация Anthropic request / response

**Files:**
- Create: `anthropic_compat.py`
- Create: `tests/test_anthropic_compat.py`
- Modify: `antigravity_proxy.py` (подключение конвертеров к существующим `_build_gemini_request`, `_extract_parts`).

**Interfaces:**
- Produces: `anthropic_request_to_openai(body: dict) -> dict` и `openai_response_to_anthropic(response: dict, model: str) -> dict`; ошибка `ValueError` на неподдерживаемый формат. Tool ID хранит upstream ID и thoughtSignature в уже принятом формате `call_<id>|<signature>`.
- Consumes: `MODEL_MAP` из Task 1; существующие `_build_gemini_request(body, backend_model, thinking_level)` и `gemini_response_to_openai(response, model)`.

- [ ] Написать тесты `test_text_and_system_to_openai` (`system="rule"`, user `text="hello"` → OpenAI system/user), `test_tool_roundtrip_preserves_name_id_signature` (assistant `tool_use` с `id="call_f1|sig1"`, `name="Bash"` + user `tool_result` на этот ID → Gemini functionCall/functionResponse `name="Bash"`, `id="f1"`, `thoughtSignature="sig1"`), `test_two_same_named_tools_stay_distinct` (ID `call_f1|sig1`, `call_f2|sig2`), `test_response_text_usage_and_stop_reason` (`STOP` → `end_turn`, токены 7/3), `test_reject_unsupported_content` (image и неизвестный block → `ValueError`).
- [ ] Запустить `python3 -m unittest tests.test_anthropic_compat -v`; ожидается FAIL.
- [ ] Реализовать конвертеры: блоки `text`, `tool_use`, `tool_result`, допустимые `thinking` на replay; raw image/file отбрасывать нельзя — возвращать `ValueError`; `tools.input_schema` → OpenAI function parameters, `stop_sequences` → `stop`; корректное соответствие ID→имя для каждой функции.
- [ ] Повторить `python3 -m unittest tests.test_anthropic_compat -v`; ожидается PASS.

### Task 3: Anthropic HTTP и SSE

**Files:**
- Modify: `antigravity_proxy.py:1166-1256` (routes), добавить `_handle_anthropic`, Anthropic SSE writer.
- Modify: `anthropic_compat.py` (конвертер Gemini event → Anthropic content events).
- Create: `tests/test_anthropic_http.py`

**Interfaces:**
- Consumes: `anthropic_request_to_openai`, `openai_response_to_anthropic` из Task 2.
- Produces: `POST /v1/messages` (и `/messages`) с JSON или SSE (`event:` + `data:`) и корректным Anthropic `error` envelope/status для отказов.

- [ ] Написать HTTP-тесты с fake upstream: `test_json_messages` (`type="message"`, `role="assistant"`, text block, usage 7/3), `test_stream_text_and_usage` (последовательность `message_start`, block start/delta/stop, `message_delta`, `message_stop`, usage-only chunk), `test_stream_multiple_same_named_tools` (два block index и уникальные ID), `test_stream_error_before_and_after_first_chunk` (до заголовков HTTP 502, после — `event: error` без `message_stop`), `test_invalid_body_rejected` (HTTP 400), `test_existing_openai_route_unchanged` (прежний `chat.completion` JSON).
- [ ] Запустить `python3 -m unittest tests.test_anthropic_http -v`; ожидается FAIL.
- [ ] Реализовать маршрут и SSE state machine: один `message_start`, возрастающие индексы content blocks, `input_json_delta` для tool, `message_delta` с `stop_reason` `tool_use`/`end_turn`/`max_tokens` и usage, завершение `message_stop`; pre-header ошибки — HTTP 4xx/5xx, post-header — SSE `event: error` без ложного успешного завершения. Текстовые дельты не дублировать при cumulative upstream chunks.
- [ ] Повторить `python3 -m unittest tests.test_anthropic_http -v`; ожидается PASS.

### Task 4: Отдельные настройки и локальная проверка

**Files:**
- Create: `~/.claude/agy-settings.json`
- Modify: `~/.bashrc:127-131` (одна строка алиаса)
- Modify: `README.md` (инструкция по запуску/проверке)
- Create: `tests/test_settings.py` (проверка expected env и alias)

**Interfaces:**
- Produces: `agy-claude` → `claude --dangerously-skip-permissions --settings ~/.claude/agy-settings.json` с неизменёнными аргументами.

- [ ] Написать `test_agy_settings_and_alias`: JSON содержит `ANTHROPIC_BASE_URL=http://127.0.0.1:8877`, `ANTHROPIC_MODEL=claude-opus-5-5`, `ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-5-5`, `ANTHROPIC_DEFAULT_HAIKU_MODEL=gemini-3.8-flash`; alias указывает только новый `--settings`, а `modelPicker` предлагает обе Claude-модели и Gemini 4 Argon / 3.8 Flash; до изменений тест падает.
- [ ] Создать новый settings-файл в стиле `kimi-settings.json` с локальным dummy token, без копирования секретов. Добавить ровно один алиас в `.bashrc`; упомянуть ручной запуск прокси.
- [ ] Запустить `python3 -m unittest discover -s tests -v`, `python3 -m py_compile antigravity_proxy.py anthropic_compat.py`, `python3 -m json.tool ~/.claude/agy-settings.json`, `bash -n ~/.bashrc` и `bash -ic 'type agy-claude'`; ожидается PASS.
- [ ] Проверить локальный HTTP smoke с mocked upstream и, если совместимо с CLI без внешних вызовов, `claude -p` с тестовым локальным сервером; любые ограничения или пропущенные проверки назвать прямо. Не стартовать постоянную службу и не отправлять реальные prompts в Google.
