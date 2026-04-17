# Variational Omni Trading Bot

Фарм поинтов через объём на BTC-USDC. 0% комиссий.

## Запуск

```bash
cd ~/Desktop/VariationalBot
source .venv/bin/activate
python main.py
```

## Файлы

- `config.txt` — все настройки (размер, плечо, TP/SL, таймауты)
- `.env` — секреты (cookie, кошелёк)
- `variational_bot.log` — лог работы
- `trades.csv` — история сделок

## Если cookie протухли

1. Открой https://omni.variational.io в браузере
2. DevTools → Application → Cookies → скопируй всё
3. Вставь в `.env` → `VARIATIONAL_COOKIE=...`
