# Combo Posting

MVP автопостинга рилсов: Instagram Reels (Graph API) + Threads (пока заглушка).
Спека: `/opt/data/projects/dailyqapp/promo/instagram_pipeline.md` (§1.5, §1.6, §7).

## Установка

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
```

## Использование

```bash
# поставить в очередь (время — Asia/Dubai, см. --tz)
.venv/bin/combo add reel.mp4 --caption $'Хук\nценность' --at '2026-09-29 18:00' \
    --to ig,threads --product dailyq --comment '#buildinpublic'
.venv/bin/combo run          # крон: */5 * * * * cd ... && .venv/bin/combo run
.venv/bin/combo status [ID]
.venv/bin/combo retry ID     # failed/processing/dry_run -> pending, сделанные шаги сохраняются
.venv/bin/combo check reel.mp4
```

Env: `IG_TOKEN`, `IG_USER_ID` (без них — DRY_RUN: печать плана, без сети), `IG_HANDLE`,
`IG_API_VERSION` (v23.0), `COMBO_PUBLIC_BASE_URL` (если задан — `video_url`, иначе resumable
upload в rupload.facebook.com), `COMBO_DRY_RUN=1`, `COMBO_DB` (по умолч. `data/combo.db`),
`COMBO_TZ`, `DAILYQ_URL`, `BOTIFY_BOT`.

## Структура

- `combo_posting/queue.py` — SQLite-очередь, статусы `pending → processing → published|failed|dry_run`,
  атомарный claim, прогресс по платформам (`results`, `ig_container_id`, `ig_media_id`).
- `combo_posting/normalize.py` — ffprobe-проверка по спеке IG + минимальное исправление:
  ремукс (faststart, edit list) / тишина AAC 48k / полное перекодирование.
- `combo_posting/captions.py` — CTA-шаблоны, UTM/deeplink, лимиты IG/Threads.
- `combo_posting/platforms/instagram.py` — квота → контейнер → upload → поллинг → publish → коммент.
- `combo_posting/platforms/threads.py` — заглушка (всегда dry-run).
- `combo_posting/runner.py`, `combo_posting/cli.py`.

## Тесты

```bash
.venv/bin/python -m pytest -q
```
