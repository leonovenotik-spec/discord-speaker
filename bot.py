"""Private, single-server Discord TTS bot. Python 3.12+."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
import json
import logging
import os
from pathlib import Path
import tempfile

import discord
from discord import app_commands
import edge_tts
import imageio_ffmpeg

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


class Preferences:
    def __init__(self, folder):
        self.path = Path(folder) / "preferences.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.voice = "ru-RU-DmitryNeural"
        self.speed = "+0%"
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    self.voice = data.get("voice") if data.get("voice") in VOICES else self.voice
                    self.speed = data.get("speed") if data.get("speed") in SPEEDS else self.speed
            except (ValueError, TypeError, OSError):
                log.warning("Preferences could not be read; using defaults")

    def set(self, *, voice=None, speed=None):
        if voice is not None and voice not in VOICES:
            raise UserError("Выбери голос из списка.")
        if speed is not None and speed not in SPEEDS:
            raise UserError("Выбери скорость из списка.")
        selected_voice = voice or self.voice
        selected_speed = speed or self.speed
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps({"voice": selected_voice, "speed": selected_speed}))
        staging.replace(self.path)
        self.voice, self.speed = selected_voice, selected_speed


class UserError(Exception):
    pass


class MuteJournal:
    """Record before muting, so a restart can recover an interrupted request."""
    def __init__(self, folder):
        self.path = Path(folder) / "pending-mute.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def record(self, member):
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps({"guild": member.guild.id, "user": member.id}))
        staging.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)

    async def restore(self, guild):
        pending = self.read()
        if not pending:
            return
        if pending["guild"] != guild.id or pending["user"] != guild.owner_id:
            raise UserError("Запись восстановления относится к другому владельцу или серверу.")
        member = await guild.fetch_member(pending["user"])
        # If disconnected, keep the record and retry when the member rejoins.
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


@asynccontextmanager
async def muted_for_speech(member, journal):
    if not member.voice or not member.voice.channel:
        raise UserError("Сначала зайди в обычный голосовой канал.")
    if member.voice.mute:
        raise UserError("На тебе уже стоит серверный мут. Сначала сними его вручную.")
    journal.record(member)
    try:
        await member.edit(mute=True, reason="TTS: микрофон выключен на время озвучки")
        yield
    finally:
        await journal.restore(member.guild)


async def play_audio(voice, source):
    loop = asyncio.get_running_loop()
    finished = loop.create_future()

    def finish(error):
        if not finished.done():
            if error:
                finished.set_exception(error)
            else:
                finished.set_result(None)

    voice.play(source, after=lambda error: loop.call_soon_threadsafe(finish, error))
    try:
        await asyncio.wait_for(finished, timeout=180)
    finally:
        voice.stop()


class Speaker(discord.Client):
    def __init__(self, guild_id, data_dir):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self.lock = asyncio.Lock()
        self.cancel = asyncio.Event()
        self.journal = MuteJournal(data_dir)
        self.preferences = Preferences(data_dir)

    async def setup_hook(self):
        self.tree.add_command(say)
        self.tree.add_command(stop)
        self.tree.add_command(restore)
        self.tree.add_command(panel)
        self.tree.copy_global_to(guild=discord.Object(id=self.guild_id))
        await self.tree.sync(guild=discord.Object(id=self.guild_id))

    async def recover(self):
        async with self.lock:
            guild = self.get_guild(self.guild_id)
            if guild and self.journal.read():
                try:
                    await self.journal.restore(guild)
                    log.info("Microphone state recovered")
                except Exception as exc:
                    log.warning("Recovery pending (%s). Use /restore after joining voice.", type(exc).__name__)

    async def on_ready(self):
        log.info("READY: /panel, /say, /stop, /restore are available")
        await self.recover()

    async def on_voice_state_update(self, member, before, after):
        if (member.guild.id == self.guild_id and member.id == member.guild.owner_id
                and after.channel and not before.channel and not self.lock.locked()):
            await self.recover()

    async def close(self):
        for voice in self.voice_clients:
            voice.stop()
        # This also covers a normal shutdown; hard crashes recover on the next start.
        try:
            await self.recover()
        finally:
            await super().close()


async def owner_only(interaction):
    client = interaction.client
    if (not interaction.guild or interaction.guild.id != client.guild_id
            or interaction.user.id != interaction.guild.owner_id):
        await interaction.response.send_message("Бот доступен только владельцу настроенного сервера.", ephemeral=True)
        return False
    return True


@app_commands.command(name="say", description="Озвучить текст, временно выключив твой микрофон")
@app_commands.guild_only()
@app_commands.describe(text="Текст для озвучки — до 500 символов", voice="Голос (запоминается)",
                       speed="Скорость (запоминается)")
@app_commands.choices(voice=[app_commands.Choice(name=name, value=value) for value, name in VOICES.items()],
                      speed=[app_commands.Choice(name=name, value=value) for value, name in SPEEDS.items()])
async def say(interaction: discord.Interaction, text: app_commands.Range[str, 1, 500],
              voice: str | None = None, speed: str | None = None):
    if not await owner_only(interaction):
        return
    client = interaction.client
    if client.lock.locked():
        await interaction.response.send_message("Уже готовлю или озвучиваю текст. Для отмены — /stop.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with client.lock:
        client.cancel.clear()
        connection = None
        source = None
        message = "Готово."
        try:
            if client.journal.read():
                await client.journal.restore(interaction.guild)
            member = interaction.user
            channel = member.voice.channel if member.voice else None
            if not isinstance(channel, discord.VoiceChannel):
                raise UserError("Зайди в обычный голосовой канал. Трибуны не поддерживаются.")
            permissions = channel.permissions_for(interaction.guild.me)
            if not all((permissions.view_channel, permissions.connect, permissions.speak, permissions.mute_members)):
                raise UserError("Выдай боту права: видеть канал, подключаться, говорить и отключать микрофон участникам.")
            if not text.strip():
                raise UserError("Напиши текст, который нужно произнести.")
            if voice is not None or speed is not None:
                client.preferences.set(voice=voice, speed=speed)
            selected_voice = client.preferences.voice
            selected_speed = client.preferences.speed
            with tempfile.TemporaryDirectory(prefix="speaker-") as folder, ExitStack() as cleanup:
                path = Path(folder) / "speech.mp3"
                await asyncio.wait_for(edge_tts.Communicate(
                    text.strip(), voice=selected_voice, rate=selected_speed).save(str(path)), timeout=60)
                if client.cancel.is_set():
                    raise UserError("Озвучка отменена.")
                # Check again after synthesis: the user may have moved or left.
                if not member.voice or member.voice.channel != channel:
                    raise UserError("Ты сменил канал. Повтори команду в новом канале.")
                connection = interaction.guild.voice_client
                if connection and connection.channel != channel:
                    await connection.move_to(channel)
                elif connection is None:
                    connection = await channel.connect(timeout=30, reconnect=True, self_deaf=True)
                source = await discord.FFmpegOpusAudio.from_probe(
                    str(path), executable=imageio_ffmpeg.get_ffmpeg_exe(), method="fallback")
                cleanup.callback(source.cleanup)
                if client.cancel.is_set():
                    raise UserError("Озвучка отменена.")
                async with muted_for_speech(member, client.journal):
                    if client.cancel.is_set():
                        raise UserError("Озвучка отменена.")
                    await play_audio(connection, source)
                if client.cancel.is_set():
                    message = "Озвучка остановлена. Микрофон восстановлен."
        except UserError as exc:
            message = str(exc)
        except discord.Forbidden:
            message = "Discord отклонил действие. Проверь права бота в этом голосовом канале."
        except Exception as exc:
            log.warning("Speech failed (%s)", type(exc).__name__)
            message = "Не удалось озвучить текст: проверь интернет и журнал хостинга."
        finally:
            current = connection or interaction.guild.voice_client
            if current:
                try:
                    await current.disconnect(force=True)
                except Exception:
                    log.warning("Voice disconnect failed")
            if client.journal.read():
                message += " Восстановление микрофона не завершено: зайди в голосовой канал и выполни /restore; либо сними серверный мут вручную."
        await interaction.edit_original_response(content=message)


@app_commands.command(name="stop", description="Остановить мою озвучку")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    interaction.client.cancel.set()
    connection = interaction.guild.voice_client
    if connection:
        connection.stop()
    await interaction.response.send_message("Отмена запрошена. Если речь уже звучала, бот восстановит микрофон.", ephemeral=True)


@app_commands.command(name="restore", description="Восстановить микрофон после сбоя бота")
@app_commands.guild_only()
async def restore(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    client = interaction.client
    if client.lock.locked():
        await interaction.response.send_message("Сначала останови озвучку командой /stop и дождись её окончания.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with client.lock:
        try:
            if not client.journal.read():
                result = "Незавершённого отключения микрофона нет."
            else:
                await client.journal.restore(interaction.guild)
                result = "Серверный мут, поставленный ботом, снят."
        except UserError as exc:
            result = str(exc)
        except discord.HTTPException:
            result = "Не удалось снять мут. Проверь права бота или сними серверный мут вручную."
        await interaction.edit_original_response(content=result)


def panel_text(client):
    prefs = client.preferences
    return (f"**Твой голос**\nГолос: {VOICES[prefs.voice]}\nСкорость: {SPEEDS[prefs.speed]}\n\n"
            "Нажми «Озвучить» и напиши фразу. Голос и скорость сохраняются.\n"
            "Микрофон отключается только на время речи.\n"
            "Кнопки действуют 15 минут; затем снова введи /panel.")


class SpeechModal(discord.ui.Modal, title="Что озвучить?"):
    text = discord.ui.TextInput(label="Текст", style=discord.TextStyle.paragraph,
                                placeholder="Напиши свою фразу…", max_length=500)

    async def on_submit(self, interaction):
        await say.callback(interaction, text=self.text.value)

    async def on_error(self, interaction, error):
        await command_error(interaction, error)


class PreferenceSelect(discord.ui.Select):
    def __init__(self, kind, preferences):
        self.kind = kind
        catalog = VOICES if kind == "voice" else SPEEDS
        current = getattr(preferences, kind)
        super().__init__(placeholder="Выбери голос" if kind == "voice" else "Выбери скорость",
                         row=0 if kind == "voice" else 1,
                         options=[discord.SelectOption(label=name, value=value, default=value == current)
                                  for value, name in catalog.items()])

    async def callback(self, interaction):
        client = interaction.client
        client.preferences.set(**{self.kind: self.values[0]})
        await interaction.response.edit_message(content=panel_text(client), view=ControlPanel(client))


class ControlPanel(discord.ui.View):
    def __init__(self, client):
        super().__init__(timeout=900)
        self.add_item(PreferenceSelect("voice", client.preferences))
        self.add_item(PreferenceSelect("speed", client.preferences))

    async def interaction_check(self, interaction):
        return await owner_only(interaction)

    @discord.ui.button(label="Озвучить", style=discord.ButtonStyle.primary, row=2)
    async def speak_button(self, interaction, button):
        if interaction.client.lock.locked():
            await interaction.response.send_message("Уже озвучиваю. Дождись окончания или нажми «Стоп».", ephemeral=True)
            return
        await interaction.response.send_modal(SpeechModal())

    @discord.ui.button(label="Стоп", style=discord.ButtonStyle.danger, row=2)
    async def stop_button(self, interaction, button):
        await stop.callback(interaction)

    async def on_error(self, interaction, error, item):
        await command_error(interaction, error)


@app_commands.command(name="panel", description="Панель озвучки: кнопки, голос и скорость")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if await owner_only(interaction):
        await interaction.response.send_message(content=panel_text(interaction.client),
                                                view=ControlPanel(interaction.client), ephemeral=True)


@say.error
@stop.error
@restore.error
@panel.error
async def command_error(interaction, error):
    log.warning("Command failed (%s)", type(error).__name__)
    message = "Команда не выполнена. Повтори её; проверь права и журнал хостинга."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    guild_id = os.environ.get("GUILD_ID", "")
    if not token or not guild_id.isdecimal():
        raise SystemExit("Set DISCORD_TOKEN and numeric GUILD_ID in hosting Variables.")
    client = Speaker(int(guild_id), os.environ.get("DATA_DIR", "data"))
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
