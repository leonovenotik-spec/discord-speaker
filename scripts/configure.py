"""Enter bot credentials locally on the Linux host, without echoing the token."""
import getpass
import os
from pathlib import Path
import re


def write_config(path, token, guild_id):
    if not token or not re.fullmatch(r"[A-Za-z0-9._-]+", token):
        raise ValueError("Токен пустой или содержит посторонние символы.")
    if not guild_id.isdecimal() or not 15 <= len(guild_id) <= 22:
        raise ValueError("ID сервера должен содержать от 15 до 22 цифр.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config:
        config.write(f"DISCORD_TOKEN={token}\nGUILD_ID={guild_id}\n")


def main():
    path = Path.home() / ".config/discord-speaker/bot.env"
    if path.exists():
        raise SystemExit(f"Настройки уже существуют: {path}. Файл не изменён.")
    print("Настройка Discord Speaker. Токен не отображается при вводе.")
    token = getpass.getpass("Вставь токен бота и нажми Enter: ").strip()
    guild_id = input("Вставь ID сервера и нажми Enter: ").strip()
    try:
        write_config(path, token, guild_id)
    except (ValueError, OSError) as error:
        raise SystemExit(f"Настройки не сохранены: {error}") from None
    print("Настройки сохранены с доступом только твоему пользователю.")
    print("Можно вернуться в чат: бот готов к запуску.")


if __name__ == "__main__":
    main()
