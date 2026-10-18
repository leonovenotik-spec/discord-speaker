import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import test_access as fixtures


class SessionTests(unittest.IsolatedAsyncioTestCase):
    interaction = fixtures.AccessTests.interaction
    voice_interaction = fixtures.AccessTests.voice_interaction

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = bot.Speaker(self.temp.name, message_content=True)
        self.source = SimpleNamespace(cleanup=Mock())
        self.tts = patch.object(bot.edge_tts, "Communicate", return_value=SimpleNamespace(save=AsyncMock()))
        self.decoder = patch.object(bot.discord, "FFmpegOpusAudio", return_value=self.source)
        self.tts.start()
        self.decoder.start()
        self.addCleanup(self.tts.stop)
        self.addCleanup(self.decoder.stop)

    async def asyncTearDown(self):
        tasks = [state.worker_task for state in self.client.states.values() if state.worker_task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def session(self):
        interaction, connection = self.voice_interaction()
        interaction.guild.voice_client = connection
        interaction.guild.get_channel = Mock(return_value=connection.channel)
        state = self.client.state(interaction.guild.id)
        state.session_owner_id = interaction.user.id
        state.session_channel_id = connection.channel.id
        return interaction, connection, state

    def message(self, interaction, text="Привет", channel_id=555):
        return SimpleNamespace(guild=interaction.guild, author=interaction.user,
            channel=SimpleNamespace(id=channel_id), clean_content=text,
            webhook_id=None, reply=AsyncMock())

    async def test_repeated_phrases_reuse_the_same_voice_connection(self):
        interaction, connection = self.voice_interaction()
        with patch.object(bot, "play_audio", new=AsyncMock()):
            await bot.say.callback(interaction, text="Один")
            await bot.say.callback(interaction, text="Два")
        connection.channel.connect.assert_awaited_once()
        connection.disconnect.assert_not_awaited()
        self.assertEqual(self.client.state(123).session_owner_id, 456)

    async def test_other_speaker_does_not_take_ownership(self):
        first, connection, state = self.session()
        second = self.interaction(user_id=777)
        second.guild = first.guild
        second.user.guild = first.guild
        second.user.voice = first.user.voice
        first.guild.fetch_member.return_value = second.user
        with patch.object(bot, "play_audio", new=AsyncMock()):
            await bot.say.callback(second, text="Другая фраза")
        self.assertEqual(state.session_owner_id, first.user.id)
        connection.disconnect.assert_not_awaited()

    async def test_owner_leaving_disconnects_even_when_other_people_remain(self):
        interaction, connection, state = self.session()
        channel = connection.channel
        channel.members = [SimpleNamespace(id=777, bot=False)]
        interaction.user.voice = None
        await self.client.on_voice_state_update(interaction.user,
            SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
        connection.disconnect.assert_awaited_once()
        self.assertIsNone(state.session_owner_id)

    async def test_other_person_leaving_does_not_disconnect_owner(self):
        interaction, connection, state = self.session()
        other = self.interaction(user_id=777).user
        other.guild = interaction.guild
        await self.client.on_voice_state_update(other,
            SimpleNamespace(channel=connection.channel), SimpleNamespace(channel=None))
        connection.disconnect.assert_not_awaited()

    async def test_empty_voice_channel_disconnects_and_clears_queue(self):
        interaction, connection, state = self.session()
        connection.channel.members = [SimpleNamespace(id=99, bot=True)]
        other = self.interaction(user_id=777).user
        other.guild = interaction.guild
        state.message_queue.put_nowait((self.message(interaction), connection.channel.id))
        await self.client.on_voice_state_update(other,
            SimpleNamespace(channel=connection.channel), SimpleNamespace(channel=None))
        connection.disconnect.assert_awaited_once()
        self.assertTrue(state.message_queue.empty())

    async def test_only_session_owner_or_manager_can_request_leave(self):
        owner, connection, state = self.session()
        other = self.interaction(user_id=777)
        other.guild = owner.guild
        await bot.leave.callback(other)
        connection.disconnect.assert_not_awaited()
        await bot.leave.callback(owner)
        connection.disconnect.assert_awaited_once()

    async def test_connection_and_synthesis_start_in_parallel(self):
        interaction, connection = self.voice_interaction()
        connecting, synthesizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def connect(**kwargs):
            connecting.set()
            await release.wait()
            interaction.guild.voice_client = connection
            return connection
        async def save(path):
            synthesizing.set()
        connection.channel.connect.side_effect = connect
        with patch.object(bot.edge_tts, "Communicate", return_value=SimpleNamespace(save=save)), \
             patch.object(bot, "play_audio", new=AsyncMock()):
            task = asyncio.create_task(bot.say.callback(interaction, text="Параллельно"))
            try:
                await asyncio.wait_for(connecting.wait(), 2)
                await asyncio.wait_for(synthesizing.wait(), 2)
            finally:
                release.set()
                await task

    async def test_owner_leaving_cancels_pending_synthesis_promptly(self):
        interaction, connection, state = self.session()
        synthesizing = asyncio.Event()
        async def save(path):
            synthesizing.set()
            await asyncio.Event().wait()
        with patch.object(bot.edge_tts, "Communicate", return_value=SimpleNamespace(save=save)):
            task = asyncio.create_task(bot.say.callback(interaction, text="Отмена подготовки"))
            await asyncio.wait_for(synthesizing.wait(), 2)
            interaction.user.voice = None
            connection.channel.members = []
            await asyncio.wait_for(self.client.on_voice_state_update(interaction.user,
                SimpleNamespace(channel=connection.channel), SimpleNamespace(channel=None)), 2)
            await asyncio.wait_for(task, 2)
        connection.disconnect.assert_awaited_once()
        self.assertIsNone(state.session_owner_id)

    async def test_owner_departure_during_playback_preserves_recovery_for_return(self):
        interaction, connection, state = self.session()
        playing, stopped = asyncio.Event(), asyncio.Event()
        connection.stop.side_effect = stopped.set
        async def play(voice, source):
            playing.set()
            await stopped.wait()
        with patch.object(bot, "play_audio", new=play):
            task = asyncio.create_task(bot.say.callback(interaction, text="Проверка восстановления"))
            await asyncio.wait_for(playing.wait(), 2)
            interaction.user.voice = None
            connection.channel.members = []
            await asyncio.wait_for(self.client.on_voice_state_update(interaction.user,
                SimpleNamespace(channel=connection.channel), SimpleNamespace(channel=None)), 2)
            await task
        self.assertIsNotNone(state.member(456).journal.read())
        interaction.guild.voice_client = None
        interaction.user.voice = SimpleNamespace(channel=connection.channel, mute=True)
        connection.channel.members = [interaction.user]
        await self.client.on_voice_state_update(interaction.user,
            SimpleNamespace(channel=None), SimpleNamespace(channel=connection.channel))
        self.assertIsNone(state.member(456).journal.read())
        self.assertEqual(interaction.user.edit.await_args_list[-1].kwargs["mute"], False)

    async def test_text_channel_is_disabled_by_default(self):
        interaction, connection = self.voice_interaction()
        await self.client.on_message(self.message(interaction))
        self.assertIsNone(self.client.state(123).worker_task)

    async def test_text_messages_are_spoken_in_order(self):
        interaction, connection = self.voice_interaction()
        state = self.client.state(123)
        state.settings.set_text_channel(555)
        with patch.object(bot, "perform_speech", new=AsyncMock(return_value="Готово.")) as speech:
            await self.client.on_message(self.message(interaction, "Один"))
            await self.client.on_message(self.message(interaction, "Два"))
            await asyncio.wait_for(state.message_queue.join(), 2)
        self.assertEqual([call.args[3] for call in speech.await_args_list], ["Один", "Два"])

    async def test_unauthorized_roles_bots_webhooks_and_other_channels_are_ignored(self):
        interaction, connection = self.voice_interaction()
        state = self.client.state(123)
        state.settings.set_text_channel(555)
        state.settings.set_role(789)
        await self.client.on_message(self.message(interaction))
        state.settings.set_role(None)
        interaction.user.bot = True
        await self.client.on_message(self.message(interaction))
        interaction.user.bot = False
        webhook = self.message(interaction)
        webhook.webhook_id = 1
        await self.client.on_message(webhook)
        await self.client.on_message(self.message(interaction, channel_id=999))
        self.assertIsNone(state.worker_task)

    async def test_role_removal_while_queued_prevents_playback(self):
        interaction, connection = self.voice_interaction()
        state = self.client.state(123)
        state.settings.set_text_channel(555)
        with patch.object(bot, "perform_speech", new=AsyncMock()) as speech:
            await self.client.on_message(self.message(interaction))
            state.settings.set_role(789)
            await asyncio.wait_for(state.message_queue.join(), 2)
        speech.assert_not_awaited()

    async def test_switching_voice_channel_while_queued_does_not_redirect_message(self):
        interaction, connection = self.voice_interaction()
        state = self.client.state(123)
        state.settings.set_text_channel(555)
        message = self.message(interaction)
        await self.client.on_message(message)
        interaction.user.voice = SimpleNamespace(channel=SimpleNamespace(id=999), mute=False)
        await asyncio.wait_for(state.message_queue.join(), 2)
        connection.channel.connect.assert_not_awaited()
        message.reply.assert_awaited_once()

    async def test_full_queue_reports_error_without_growing(self):
        interaction, connection = self.voice_interaction()
        state = self.client.state(123)
        state.settings.set_text_channel(555)
        state.worker_task = asyncio.create_task(asyncio.Event().wait())
        for _ in range(20):
            await self.client.on_message(self.message(interaction))
        overflow = self.message(interaction)
        await self.client.on_message(overflow)
        self.assertEqual(state.message_queue.qsize(), 20)
        overflow.reply.assert_awaited_once()

    async def test_text_channel_setting_survives_role_changes_and_restart(self):
        state = self.client.state(123)
        state.settings.set_text_channel(555)
        state.settings.set_role(789)
        reopened = bot.Speaker(self.temp.name)
        self.assertEqual(reopened.state(123).settings.text_channel_id, 555)
        self.assertEqual(reopened.state(123).settings.role_id, 789)

    async def test_only_managers_can_configure_text_channel(self):
        interaction = self.interaction()
        await bot.textchannel.callback(interaction, disable=True)
        self.assertFalse(self.client.state(123).settings.path.exists())
        manager = self.interaction(manager=True)
        channel = SimpleNamespace(id=555, guild=manager.guild,
            permissions_for=lambda member: SimpleNamespace(view_channel=True))
        manager.guild.me = object()
        await bot.textchannel.callback(manager, channel=channel)
        self.assertEqual(self.client.state(123).settings.text_channel_id, 555)
        await bot.textchannel.callback(manager, disable=True)
        self.assertIsNone(self.client.state(123).settings.text_channel_id)
