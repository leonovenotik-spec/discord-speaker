"""Persistent settings and per-member recovery state, isolated by Discord guild."""
import asyncio
import json
import logging
from pathlib import Path

import discord

log = logging.getLogger("speaker")

VOICES = {
    "ru-RU-DmitryNeural": "Дмитрий — мужской",
    "ru-RU-SvetlanaNeural": "Светлана — женский",
    "en-US-AndrewMultilingualNeural": "Эндрю — мужской, многоязычный",
    "en-US-BrianMultilingualNeural": "Брайан — мужской, многоязычный",
    "en-US-AvaMultilingualNeural": "Ава — женский, многоязычный",
    "en-US-EmmaMultilingualNeural": "Эмма — женский, многоязычный",
}
SPEEDS = {"-30%": "Медленно", "-15%": "Чуть медленнее", "+0%": "Обычно",
          "+15%": "Чуть быстрее", "+30%": "Быстро"}


class UserError(Exception):
    pass


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(data), encoding="utf-8")
    staging.replace(path)


class Preferences:
    def __init__(self, folder):
        self.path = Path(folder) / "preferences.json"
        self.voice = "ru-RU-DmitryNeural"
        self.speed = "+0%"
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if isinstance(data.get("voice"), str) and data["voice"] in VOICES:
                        self.voice = data["voice"]
                    if isinstance(data.get("speed"), str) and data["speed"] in SPEEDS:
                        self.speed = data["speed"]
            except (ValueError, OSError):
                log.warning("Preferences could not be read; using defaults")

    def set(self, *, voice=None, speed=None):
        if voice is not None and voice not in VOICES:
            raise UserError("Выбери голос из списка.")
        if speed is not None and speed not in SPEEDS:
            raise UserError("Выбери скорость из списка.")
        selected_voice = voice or self.voice
        selected_speed = speed or self.speed
        write_json(self.path, {"voice": selected_voice, "speed": selected_speed})
        self.voice, self.speed = selected_voice, selected_speed


class ServerSettings:
    def __init__(self, folder):
        self.path = Path(folder) / "settings.json"
        self.role_id = None
        self.text_channel_id = None
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                role_id = data["role_id"]
                if role_id is not None and (type(role_id) is not int or role_id <= 0):
                    raise ValueError("Invalid role")
                self.role_id = role_id
                channel_id = data.get("text_channel_id")
                if channel_id is not None and (type(channel_id) is not int or channel_id <= 0):
                    raise ValueError("Invalid text channel")
                self.text_channel_id = channel_id
            except (ValueError, KeyError, TypeError, OSError):
                # An unreadable access policy must not silently open access to everyone.
                raise UserError("Настройки доступа сервера повреждены. Обратись к владельцу бота.") from None

    def set_role(self, role_id):
        if role_id is not None and (type(role_id) is not int or role_id <= 0):
            raise UserError("Некорректная роль.")
        write_json(self.path, {"role_id": role_id, "text_channel_id": self.text_channel_id})
        self.role_id = role_id

    def set_text_channel(self, channel_id):
        if channel_id is not None and (type(channel_id) is not int or channel_id <= 0):
            raise UserError("Некорректный текстовый канал.")
        write_json(self.path, {"role_id": self.role_id, "text_channel_id": channel_id})
        self.text_channel_id = channel_id


class MuteJournal:
    def __init__(self, folder):
        self.path = Path(folder) / "pending-mute.json"

    def read(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if any(type(data[key]) is not int or data[key] <= 0 for key in ("guild", "user")):
                raise ValueError("Invalid member")
            return data
        except (ValueError, KeyError, TypeError, OSError):
            raise UserError("Запись восстановления повреждена. Сними серверный мут вручную.") from None

    def record(self, member):
        write_json(self.path, {"guild": member.guild.id, "user": member.id})

    def clear(self):
        self.path.unlink(missing_ok=True)

    async def restore(self, guild, expected_user_id):
        pending = self.read()
        if not pending:
            return
        if pending["guild"] != guild.id or pending["user"] != expected_user_id:
            raise UserError("Запись восстановления относится к другому участнику или серверу.")
        member = await guild.fetch_member(expected_user_id)
        if not member.voice or not member.voice.channel:
            raise UserError("Зайди в голосовой канал и выполни /restore.")
        for attempt in range(3):
            try:
                await member.edit(mute=False, reason="TTS: восстановление после озвучки")
                self.clear()
                return
            except discord.HTTPException:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)


class MemberState:
    def __init__(self, folder):
        self.preferences = Preferences(folder)
        self.journal = MuteJournal(folder)


class GuildState:
    def __init__(self, root, guild_id):
        self.folder = Path(root) / "guilds" / str(guild_id)
        self.settings = ServerSettings(self.folder)
        self.lock = asyncio.Lock()
        self.cancel = asyncio.Event()
        self.active_user_id = None
        self.session_owner_id = None
        self.session_channel_id = None
        self.session_ending = False
        self.message_queue = asyncio.Queue(maxsize=20)
        self.worker_task = None
        self.members = {}

    def member(self, user_id):
        if user_id not in self.members:
            self.members[user_id] = MemberState(self.folder / "members" / str(user_id))
        return self.members[user_id]

    def pending_users(self):
        for path in (self.folder / "members").glob("*/pending-mute.json"):
            if path.parent.name.isdecimal():
                yield int(path.parent.name)
