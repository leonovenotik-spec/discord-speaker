"""Multi-server Discord TTS bot with optional role-based access. Python 3.12+."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
import logging
import os
from pathlib import Path
import tempfile
import time

import discord
from discord import app_commands
import edge_tts
import imageio_ffmpeg

from storage import GuildState, MuteJournal, Preferences, SPEEDS, UserError, VOICES, write_json

log = logging.getLogger("speaker")


def is_manager(guild, member):
    return (member.id == guild.owner_id or member.guild_permissions.manage_guild
            or member.guild_permissions.administrator)


def can_speak(guild, member, settings):
    return (settings.role_id is None or is_manager(guild, member)
            or any(role.id == settings.role_id for role in member.roles))


async def require_access(interaction, *, recovery=False):
    if interaction.client.shutting_down:
        await interaction.response.send_message("Бот перезапускается. Повтори команду немного позже.", ephemeral=True)
        return False
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает на сервере Discord.", ephemeral=True)
        return False
    state = interaction.client.state(interaction.guild.id)
    if not recovery and not can_speak(interaction.guild, interaction.user, state.settings):
        await interaction.response.send_message(
            "Для озвучки нужна роль, выбранная администратором сервера. Настройка: /access.", ephemeral=True)
        return False
    return True


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
        await journal.restore(member.guild, member.id)


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


def clear_message_queue(state):
    while not state.message_queue.empty():
        state.message_queue.get_nowait()
        state.message_queue.task_done()


async def disconnect_session(guild, state):
    state.session_ending = True
    state.cancel.set()
    state.session_owner_id = None
    state.session_channel_id = None
    clear_message_queue(state)
    connection = guild.voice_client
    try:
        if connection:
            connection.stop()
            await connection.disconnect(force=True)
    except (discord.HTTPException, discord.ClientException):
        log.warning("Voice disconnect failed")
    finally:
        state.session_ending = False


async def wait_or_cancel(awaitable, cancel_event, timeout):
    task = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait((task, cancellation), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done:
            raise UserError("Озвучка отменена.")
        if task not in done:
            raise TimeoutError("Speech preparation timed out")
        return await task
    finally:
        for pending in (task, cancellation):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, cancellation, return_exceptions=True)


async def ensure_connection(guild, member, channel, state):
    connection = guild.voice_client
    if connection and not connection.is_connected():
        await connection.disconnect(force=True)
        connection = None
    if connection:
        if connection.channel != channel:
            raise UserError("Бот уже занят в другом голосовом канале. Дождись выхода вызвавшего его участника.")
        if state.session_owner_id is None:
            state.session_owner_id = member.id
            state.session_channel_id = channel.id
        return connection
    if not member.voice or member.voice.channel != channel:
        raise UserError("Ты уже вышел из голосового канала.")
    state.session_owner_id = member.id
    state.session_channel_id = channel.id
    try:
        connection = await channel.connect(timeout=30, reconnect=True, self_deaf=True)
        if state.cancel.is_set():
            raise UserError("Озвучка отменена.")
        return connection
    except (Exception, asyncio.CancelledError):
        await disconnect_session(guild, state)
        raise


async def perform_speech(client, guild, member, text, *, voice=None, speed=None,
                         expected_channel_id=None, on_started=None):
    """Caller holds the guild lock; text is kept in memory only for this request."""
    state = client.state(guild.id)
    if state.session_ending or client.shutting_down:
        return "Сеанс завершается. Повтори сообщение после выхода бота."
    state.active_user_id = member.id
    state.cancel.clear()
    user_state = state.member(member.id)
    connect_task = None
    message = "Готово."
    start = time.monotonic()
    try:
        if user_state.journal.read():
            await user_state.journal.restore(guild, member.id)
        channel = member.voice.channel if member.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            raise UserError("Зайди в обычный голосовой канал. Трибуны не поддерживаются.")
        if expected_channel_id is not None and channel.id != expected_channel_id:
            raise UserError("Ты сменил голосовой канал; сообщение из очереди пропущено.")
        if not can_speak(guild, member, state.settings):
            raise UserError("Для озвучки нужна роль, выбранная администратором сервера.")
        permissions = channel.permissions_for(guild.me)
        if not all((permissions.view_channel, permissions.connect, permissions.speak, permissions.mute_members)):
            raise UserError("Выдай боту права: видеть канал, подключаться, говорить и отключать микрофон участникам.")
        if not text.strip() or len(text) > 500:
            raise UserError("Напиши от 1 до 500 символов для озвучки.")
        if voice is not None or speed is not None:
            user_state.preferences.set(voice=voice, speed=speed)
        with tempfile.TemporaryDirectory(prefix="speaker-") as folder, ExitStack() as cleanup:
            path = Path(folder) / "speech.mp3"
            # The first connection and synthesis overlap; subsequent phrases reuse voice.
            connect_task = asyncio.create_task(ensure_connection(guild, member, channel, state))
            synth_start = time.monotonic()
            await wait_or_cancel(edge_tts.Communicate(
                text.strip(), voice=user_state.preferences.voice, rate=user_state.preferences.speed
            ).save(str(path)), state.cancel, timeout=45)
            synth_seconds = time.monotonic() - synth_start
            connection = await wait_or_cancel(connect_task, state.cancel, timeout=30)
            if state.cancel.is_set():
                raise UserError("Озвучка отменена.")
            member = await guild.fetch_member(member.id)
            if not can_speak(guild, member, state.settings):
                raise UserError("Доступ к озвучке изменился. Обратись к администратору сервера.")
            if not member.voice or member.voice.channel != channel:
                raise UserError("Ты сменил канал. Повтори команду в новом канале.")
            # The input format is known (MP3): no extra probing subprocess is needed.
            source = discord.FFmpegOpusAudio(str(path), executable=client.ffmpeg_executable)
            cleanup.callback(source.cleanup)
            if state.cancel.is_set():
                raise UserError("Озвучка отменена.")
            if on_started:
                try:
                    await on_started()
                except discord.HTTPException:
                    pass
            log.info("Speech ready in %.2fs (synthesis %.2fs)", time.monotonic() - start, synth_seconds)
            async with muted_for_speech(member, user_state.journal):
                if state.cancel.is_set():
                    raise UserError("Озвучка отменена.")
                await play_audio(connection, source)
            if state.cancel.is_set():
                message = "Озвучка остановлена. Микрофон восстановлен."
    except UserError as exc:
        message = str(exc)
    except discord.Forbidden:
        message = "Discord отклонил действие. Проверь права бота в этом голосовом канале."
    except Exception as exc:
        log.warning("Speech failed (%s)", type(exc).__name__)
        message = "Не удалось озвучить текст: проверь интернет и журнал хостинга."
    finally:
        if connect_task:
            if not connect_task.done():
                connect_task.cancel()
            await asyncio.gather(connect_task, return_exceptions=True)
        state.active_user_id = None
        if user_state.journal.path.exists():
            message += " Восстановление микрофона не завершено: зайди в голосовой канал и выполни /restore; либо сними серверный мут вручную."
    return message


class Speaker(discord.Client):
    def __init__(self, data_dir, legacy_guild_id=None, *, message_content=False):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.message_content = message_content
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=None)
        self.ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()
        self.data_dir = Path(data_dir)
        self.states = {}
        self.shutting_down = False
        self.synced_guild_ids = set()
        self.legacy_guild_id = legacy_guild_id
        self.tree = app_commands.CommandTree(
            self, allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False))
        self.migrate_pending_mute()

    def state(self, guild_id):
        if guild_id not in self.states:
            self.states[guild_id] = GuildState(self.data_dir, guild_id)
        return self.states[guild_id]

    def migrate_pending_mute(self):
        legacy = MuteJournal(self.data_dir)
        pending = legacy.read()
        if pending:
            target = self.state(pending["guild"]).member(pending["user"]).journal
            existing = target.read()
            if existing is not None and existing != pending:
                raise UserError("Конфликт записей восстановления. Сними серверный мут вручную.")
            write_json(target.path, pending)
            legacy.path.replace(legacy.path.with_suffix(".migrated"))

    async def setup_hook(self):
        # Remove obsolete global commands before loading local templates. Commands
        # are published only per guild, so Discord cannot show both scope copies.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        for command in (say, stop, restore, panel, access, leave, textchannel):
            self.tree.add_command(command)
        if self.legacy_guild_id:
            old_guild = discord.Object(id=self.legacy_guild_id)
            self.tree.clear_commands(guild=old_guild)
            try:
                await self.tree.sync(guild=old_guild)
            except discord.HTTPException:
                log.warning("Could not remove legacy guild commands; they will be refreshed on ready")

    async def recover(self, guild):
        state = self.state(guild.id)
        async with state.lock:
            for user_id in list(state.pending_users()):
                try:
                    await state.member(user_id).journal.restore(guild, user_id)
                    log.info("Microphone state recovered")
                except Exception as exc:
                    log.warning("Recovery pending (%s). Use /restore after joining voice.", type(exc).__name__)

    async def sync_guild_commands(self, guild):
        if guild.id in self.synced_guild_ids:
            return
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.synced_guild_ids.add(guild.id)

    async def on_guild_join(self, guild):
        await self.sync_guild_commands(guild)

    async def on_guild_remove(self, guild):
        self.synced_guild_ids.discard(guild.id)

    async def on_ready(self):
        for guild in self.guilds:
            try:
                await self.sync_guild_commands(guild)
            except discord.HTTPException as exc:
                log.warning("Guild command refresh unavailable (%s); will retry on reconnect", type(exc).__name__)
            try:
                await self.recover(guild)
            except Exception as exc:
                log.warning("Guild recovery unavailable (%s)", type(exc).__name__)
        log.info("READY: /panel, /say, /stop, /restore, /access, /leave, /textchannel are available")

    async def on_voice_state_update(self, member, before, after):
        state = self.state(member.guild.id)
        if before.channel != after.channel and state.session_channel_id is not None:
            channel_id = state.session_channel_id
            owner_left = (member.id == state.session_owner_id and before.channel is not None
                          and before.channel.id == channel_id)
            bot_moved = (self.user is not None and member.id == self.user.id
                         and before.channel is not None and before.channel.id == channel_id)
            channel = member.guild.get_channel(channel_id)
            empty = channel is not None and not any(not person.bot for person in channel.members)
            if owner_left or bot_moved or empty:
                state.session_ending = True
                state.cancel.set()
                connection = member.guild.voice_client
                if connection:
                    connection.stop()
                async with state.lock:
                    await disconnect_session(member.guild, state)
        if after.channel and not before.channel:
            if not state.lock.locked() and state.member(member.id).journal.path.exists():
                await self.recover(member.guild)

    async def reply_message_error(self, message, result):
        try:
            await message.reply(result, mention_author=False)
        except discord.HTTPException:
            log.warning("Cannot send text-channel feedback")

    async def on_message(self, message):
        if (self.shutting_down or not self.intents.message_content or message.guild is None
                or message.author.bot or message.webhook_id is not None):
            return
        state = self.state(message.guild.id)
        if state.settings.text_channel_id != message.channel.id:
            return
        if state.session_ending:
            return
        text = message.clean_content.strip()
        if not text or not can_speak(message.guild, message.author, state.settings):
            return
        channel = message.author.voice.channel if message.author.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            return
        if len(text) > 500:
            await self.reply_message_error(message, "Для озвучки напиши не более 500 символов.")
            return
        if state.session_channel_id is not None and state.session_channel_id != channel.id:
            await self.reply_message_error(message, "Бот уже работает в другом голосовом канале.")
            return
        try:
            state.message_queue.put_nowait((message, channel.id))
        except asyncio.QueueFull:
            await self.reply_message_error(message, "Очередь заполнена. Подожди немного и повтори сообщение.")
            return
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(self.message_worker(message.guild))

    async def message_worker(self, guild):
        state = self.state(guild.id)
        while True:
            message, channel_id = await state.message_queue.get()
            try:
                async with state.lock:
                    if state.settings.text_channel_id != message.channel.id:
                        continue
                    member = await guild.fetch_member(message.author.id)
                    if not can_speak(guild, member, state.settings):
                        continue
                    result = await perform_speech(self, guild, member, message.clean_content,
                                                  expected_channel_id=channel_id)
                if result != "Готово.":
                    await self.reply_message_error(message, result)
            except Exception as exc:
                log.warning("Queued speech failed (%s)", type(exc).__name__)
                await self.reply_message_error(message, "Не удалось озвучить сообщение. Проверь голосовой канал и права бота.")
            finally:
                state.message_queue.task_done()

    async def close(self):
        self.shutting_down = True
        for state in self.states.values():
            state.cancel.set()
        for voice in list(self.voice_clients):
            voice.stop()
        workers = [state.worker_task for state in self.states.values()
                   if state.worker_task is not None and not state.worker_task.done()]
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        try:
            for guild in self.guilds:
                try:
                    await self.recover(guild)
                    await disconnect_session(guild, self.state(guild.id))
                except Exception as exc:
                    log.warning("Shutdown recovery pending (%s)", type(exc).__name__)
        finally:
            await super().close()


@app_commands.command(name="say", description="Озвучить текст, временно выключив твой микрофон")
@app_commands.guild_only()
@app_commands.describe(text="Текст для озвучки — до 500 символов", voice="Твой голос (запоминается)",
                       speed="Твоя скорость (запоминается)")
@app_commands.choices(voice=[app_commands.Choice(name=name, value=value) for value, name in VOICES.items()],
                      speed=[app_commands.Choice(name=name, value=value) for value, name in SPEEDS.items()])
async def say(interaction: discord.Interaction, text: app_commands.Range[str, 1, 500],
              voice: str | None = None, speed: str | None = None):
    if not await require_access(interaction):
        return
    client = interaction.client
    state = client.state(interaction.guild.id)
    if state.lock.locked() or not state.message_queue.empty():
        await interaction.response.send_message(
            "На этом сервере уже готовится или звучит фраза. Дождись окончания.", ephemeral=True)
        return
    async with state.lock:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async def started():
            await interaction.edit_original_response(content="Озвучиваю…")
        result = await perform_speech(client, interaction.guild, interaction.user, text,
                                      voice=voice, speed=speed, on_started=started)
        await interaction.edit_original_response(content=result)


@app_commands.command(name="stop", description="Остановить свою озвучку; администратор может остановить любую")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if not await require_access(interaction, recovery=True):
        return
    state = interaction.client.state(interaction.guild.id)
    if state.active_user_id is None:
        await interaction.response.send_message("На этом сервере сейчас нет озвучки.", ephemeral=True)
        return
    if state.active_user_id != interaction.user.id and not is_manager(interaction.guild, interaction.user):
        await interaction.response.send_message("Остановить чужую озвучку может только администратор сервера.", ephemeral=True)
        return
    state.cancel.set()
    connection = interaction.guild.voice_client
    if connection:
        connection.stop()
    await interaction.response.send_message("Отмена запрошена. Бот восстановит микрофон после остановки.", ephemeral=True)


@app_commands.command(name="leave", description="Отключить бота от голосового канала и очистить очередь")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not await require_access(interaction, recovery=True):
        return
    state = interaction.client.state(interaction.guild.id)
    if state.session_owner_id is None:
        await interaction.response.send_message("Бот сейчас не находится в голосовом канале.", ephemeral=True)
        return
    if state.session_owner_id != interaction.user.id and not is_manager(interaction.guild, interaction.user):
        await interaction.response.send_message("Выйти может попросить вызвавший бота участник или управляющий сервером.", ephemeral=True)
        return
    state.session_ending = True
    state.cancel.set()
    connection = interaction.guild.voice_client
    if connection:
        connection.stop()
    await interaction.response.defer(ephemeral=True)
    async with state.lock:
        await disconnect_session(interaction.guild, state)
    await interaction.edit_original_response(content="Бот вышел из голосового канала. Очередь очищена.")


@app_commands.command(name="textchannel", description="Выбрать текстовый канал для автоматической озвучки")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="Канал, новые сообщения из которого нужно озвучивать", disable="Выключить автоозвучку")
async def textchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                      disable: bool = False):
    if interaction.guild is None or not is_manager(interaction.guild, interaction.user):
        await interaction.response.send_message("Нужно право «Управлять сервером».", ephemeral=True)
        return
    state = interaction.client.state(interaction.guild.id)
    if channel is not None and disable:
        await interaction.response.send_message("Выбери канал или отключение — один вариант.", ephemeral=True)
        return
    if channel is not None:
        if channel.guild.id != interaction.guild.id or not channel.permissions_for(interaction.guild.me).view_channel:
            await interaction.response.send_message("Выбери текстовый канал этого сервера, который виден боту.", ephemeral=True)
            return
        state.settings.set_text_channel(channel.id)
        clear_message_queue(state)
    elif disable:
        state.settings.set_text_channel(None)
        clear_message_queue(state)
    if state.settings.text_channel_id is None:
        result = "Автоозвучка сообщений выключена. Можно использовать /panel и /say."
    else:
        result = (f"Канал озвучки: <#{state.settings.text_channel_id}>. Пиши туда, находясь в голосовом канале. "
                  "Доступ по роли сохраняется; сообщения ботов и вложения не озвучиваются.")
        if not interaction.client.intents.message_content:
            result += " Владелец бота должен включить Message Content Intent в Developer Portal и перезапустить бота."
    await interaction.response.send_message(result, ephemeral=True)


@app_commands.command(name="restore", description="Восстановить свой микрофон после сбоя бота")
@app_commands.guild_only()
async def restore(interaction: discord.Interaction):
    # Recovery stays available even if the member's required role was removed.
    if not await require_access(interaction, recovery=True):
        return
    state = interaction.client.state(interaction.guild.id)
    if state.lock.locked():
        await interaction.response.send_message("Дождись окончания озвучки на сервере; свою можно остановить через /stop.", ephemeral=True)
        return
    async with state.lock:
        await interaction.response.defer(ephemeral=True)
        journal = state.member(interaction.user.id).journal
        try:
            if not journal.read():
                result = "Незавершённого отключения твоего микрофона нет."
            else:
                await journal.restore(interaction.guild, interaction.user.id)
                result = "Серверный мут, поставленный ботом, снят."
        except UserError as exc:
            result = str(exc)
        except discord.HTTPException:
            result = "Не удалось снять мут. Проверь права бота или сними серверный мут вручную."
    await interaction.edit_original_response(content=result)


@app_commands.command(name="access", description="Настроить доступ к озвучке на этом сервере")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(role="Роль, необходимая для озвучки", everyone="Открыть озвучку всем участникам")
async def access(interaction: discord.Interaction, role: discord.Role | None = None, everyone: bool = False):
    if interaction.guild is None:
        await interaction.response.send_message("Эта настройка доступна на сервере.", ephemeral=True)
        return
    if not is_manager(interaction.guild, interaction.user):
        await interaction.response.send_message("Нужно право «Управлять сервером».", ephemeral=True)
        return
    settings = interaction.client.state(interaction.guild.id).settings
    if role is not None and everyone:
        await interaction.response.send_message("Выбери роль или доступ для всех — один вариант.", ephemeral=True)
        return
    if role is not None:
        if role.guild.id != interaction.guild.id or role.is_default():
            await interaction.response.send_message("Выбери обычную роль этого сервера. Для всех: /access everyone:True.", ephemeral=True)
            return
        settings.set_role(role.id)
    elif everyone:
        settings.set_role(None)
    if settings.role_id is None:
        result = "Озвучка доступна всем участникам этого сервера."
    else:
        selected = interaction.guild.get_role(settings.role_id)
        label = selected.name if selected else "удалённая роль — выбери новую или открой доступ всем"
        result = f"Озвучка доступна участникам с ролью «{label}» и тем, у кого есть право «Управлять сервером»."
    await interaction.response.send_message(result, ephemeral=True)


def panel_text(client, guild_id, user_id):
    prefs = client.state(guild_id).member(user_id).preferences
    return (f"**Твой голос**\nГолос: {VOICES[prefs.voice]}\nСкорость: {SPEEDS[prefs.speed]}\n\n"
            "Нажми «Озвучить» и напиши фразу. Твои голос и скорость сохраняются на этом сервере.\n"
            "Твой микрофон отключается только на время твоей речи.\n"
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
        client.state(interaction.guild.id).member(interaction.user.id).preferences.set(**{self.kind: self.values[0]})
        await interaction.response.edit_message(
            content=panel_text(client, interaction.guild.id, interaction.user.id),
            view=ControlPanel(client, interaction.guild.id, interaction.user.id))


class ControlPanel(discord.ui.View):
    def __init__(self, client, guild_id, user_id):
        super().__init__(timeout=900)
        self.guild_id, self.user_id = guild_id, user_id
        prefs = client.state(guild_id).member(user_id).preferences
        self.add_item(PreferenceSelect("voice", prefs))
        self.add_item(PreferenceSelect("speed", prefs))

    async def interaction_check(self, interaction):
        if (not interaction.guild or interaction.guild.id != self.guild_id
                or interaction.user.id != self.user_id):
            await interaction.response.send_message("Открой свою панель командой /panel.", ephemeral=True)
            return False
        is_stop = (interaction.data or {}).get("custom_id") == "speaker:stop"
        return await require_access(interaction, recovery=is_stop)

    @discord.ui.button(label="Озвучить", style=discord.ButtonStyle.primary, row=2)
    async def speak_button(self, interaction, button):
        if interaction.client.state(interaction.guild.id).lock.locked():
            await interaction.response.send_message("На сервере уже озвучивается фраза. Дождись окончания.", ephemeral=True)
            return
        await interaction.response.send_modal(SpeechModal())

    @discord.ui.button(label="Стоп", style=discord.ButtonStyle.danger, row=2, custom_id="speaker:stop")
    async def stop_button(self, interaction, button):
        await stop.callback(interaction)

    async def on_error(self, interaction, error, item):
        await command_error(interaction, error)


@app_commands.command(name="panel", description="Панель озвучки: кнопки, голос и скорость")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if await require_access(interaction):
        await interaction.response.send_message(
            content=panel_text(interaction.client, interaction.guild.id, interaction.user.id),
            view=ControlPanel(interaction.client, interaction.guild.id, interaction.user.id), ephemeral=True)


@say.error
@stop.error
@restore.error
@panel.error
@access.error
@leave.error
@textchannel.error
async def command_error(interaction, error):
    log.warning("Command failed (%s)", type(error).__name__)
    message = str(error) if isinstance(error, UserError) else "Команда не выполнена. Повтори её; проверь права и журнал хостинга."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def run_bot(token, data_dir, legacy_guild_id):
    # Probe the official application flags before requesting a privileged intent.
    # Slash commands remain usable when message-content access is not enabled.
    async with discord.Client(intents=discord.Intents.none()) as probe:
        await probe.login(token)
        application = await probe.application_info()
        content_enabled = (application.flags.gateway_message_content
                           or application.flags.gateway_message_content_limited)
    log.info("Text channel support: %s", "enabled" if content_enabled else "disabled in Developer Portal")
    async with Speaker(data_dir, legacy_guild_id, message_content=content_enabled) as client:
        await client.start(token)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in hosting Variables or the server configuration file.")
    # Old installations may retain GUILD_ID; it is used only to remove old guild commands.
    old_id = os.environ.get("GUILD_ID", "")
    try:
        asyncio.run(run_bot(token, os.environ.get("DATA_DIR", "data"), int(old_id) if old_id.isdecimal() else None))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
