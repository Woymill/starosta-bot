# starosta_bot

Телеграм-бот старосты класса (aiogram 3, SQLite).

Переменные окружения: см. `.env.example`. Запуск: `python bot.py`.

База: `DB_PATH` (по умолчанию `class.db`). Копия базы приходит админу в Telegram каждый день
(`BACKUP_HOUR`) и по команде `/backup`. Восстановление: админ отправляет боту файл `.db`
с подписью `/restore`.
