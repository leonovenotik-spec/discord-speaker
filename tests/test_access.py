import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import bot
from storage import ServerSettings


class AccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = bot.Speaker(self.temp.name)

    def interaction(self, guild_id=123, user_id=456, *, roles=(), manager=False):
        guild = SimpleNamespace(id=guild_id, owner_id=1, voice_client=None,
                                get_role=Mock(return_value=SimpleNamespace(name="Озвучка")))
        member = SimpleNamespace(id=user_id, guild=guild,
            guild_permissions=discord.Permissions(manage_guild=manager),
            roles=[SimpleNamespace(id=role) for role in roles],
            voice=None, edit=AsyncMock())
        guild.fetch_member = AsyncMock(return_value=member)
        return SimpleNamespace(client=self.client, guild=guild, user=member, data={},
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                     is_done=Mock(return_value=True)),
            edit_original_response=AsyncMock())

    def voice_interaction(self, guild_id=123, user_id=456):
        interaction = self.interaction(guild_id, user_id)
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, connect=True, speak=True, mute_members=True)
        connection = SimpleNamespace(channel=channel, disconnect=AsyncMock())
        channel.connect = AsyncMock(return_value=connection)
        interaction.user.voice = SimpleNamespace(channel=channel, mute=False)
        interaction.guild.me = object()
        return interaction, connection

    async def test_default_access_accepts_non_owner(self):
        self.assertTrue(await bot.require_access(self.interaction()))

    async def test_role_is_required_after_configuration(self):
        self.client.state(123).settings.set_role(789)
        denied = self.interaction()
        self.assertFalse(await bot.require_access(denied))
        self.assertTrue(await bot.require_access(self.interaction(roles=(789,))))

    async def test_manager_can_use_bot_with_role_restriction(self):
        self.client.state(123).settings.set_role(789)
        self.assertTrue(await bot.require_access(self.interaction(manager=True)))

    async def test_deleted_role_does_not_open_access(self):
        self.client.state(123).settings.set_role(789)
        interaction = self.interaction()
        interaction.guild.get_role.return_value = None
        self.assertFalse(await bot.require_access(interaction))

    async def test_only_managers_can_change_access(self):
        await bot.access.callback(self.interaction(), everyone=True)
        self.assertFalse(self.client.state(123).settings.path.exists())

    async def test_manager_can_select_role_and_reopen_access(self):
        interaction = self.interaction(manager=True)
        role = SimpleNamespace(id=789, guild=interaction.guild, is_default=lambda: False)
        await bot.access.callback(interaction, role=role)
        self.assertEqual(self.client.state(123).settings.role_id, 789)
        reopened = bot.Speaker(self.temp.name)
        self.assertEqual(reopened.state(123).settings.role_id, 789)
        await bot.access.callback(self.interaction(manager=True), everyone=True)
        self.assertIsNone(self.client.state(123).settings.role_id)

    async def test_foreign_role_does_not_change_policy(self):
        interaction = self.interaction(manager=True)
        role = SimpleNamespace(id=789, guild=SimpleNamespace(id=999), is_default=lambda: False)
        await bot.access.callback(interaction, role=role)
        self.assertIsNone(self.client.state(123).settings.role_id)

    async def test_corrupt_role_policy_fails_closed(self):
        folder = Path(self.temp.name) / "broken"
        folder.mkdir()
        (folder / "settings.json").write_text("invalid json")
        with self.assertRaises(bot.UserError):
            ServerSettings(folder)

    async def test_settings_and_preferences_are_isolated(self):
        first = self.client.state(123)
        second = self.client.state(999)
        first.settings.set_role(789)
        first.member(456).preferences.set(voice="en-US-EmmaMultilingualNeural", speed="+30%")
        self.assertIsNone(second.settings.role_id)
        self.assertEqual(second.member(456).preferences.voice, "ru-RU-DmitryNeural")
        self.assertEqual(first.member(777).preferences.speed, "+0%")
        reopened = bot.Speaker(self.temp.name)
        self.assertEqual(reopened.state(123).member(456).preferences.speed, "+30%")

    async def test_cannot_stop_another_member(self):
        self.client.state(123).active_user_id = 777
        await bot.stop.callback(self.interaction())
        self.assertFalse(self.client.state(123).cancel.is_set())

    async def test_owner_of_phrase_and_manager_can_stop(self):
        self.client.state(123).active_user_id = 456
        await bot.stop.callback(self.interaction())
        self.assertTrue(self.client.state(123).cancel.is_set())
        self.client.state(123).cancel.clear()
        await bot.stop.callback(self.interaction(user_id=999, manager=True))
        self.assertTrue(self.client.state(123).cancel.is_set())

    async def test_recovery_available_after_required_role_removed(self):
        interaction = self.interaction()
        self.client.state(123).settings.set_role(789)
        interaction.user.voice = SimpleNamespace(channel=object(), mute=True)
        journal = self.client.state(123).member(456).journal
        journal.record(interaction.user)
        await bot.restore.callback(interaction)
        interaction.user.edit.assert_awaited_once()
        self.assertIsNone(journal.read())

    async def test_recovery_never_touches_other_members(self):
        other = self.interaction(user_id=777)
        self.client.state(123).member(777).journal.record(other.user)
        await bot.restore.callback(self.interaction())
        self.assertIsNotNone(self.client.state(123).member(777).journal.read())
        other.user.edit.assert_not_awaited()

    async def test_stop_button_still_works_after_role_removal(self):
        interaction = self.interaction()
        interaction.data = {"custom_id": "speaker:stop"}
        self.client.state(123).settings.set_role(789)
        panel = bot.ControlPanel(self.client, 123, 456)
        self.assertTrue(await panel.interaction_check(interaction))
        panel.stop()

    async def test_legacy_pending_mute_is_migrated_without_owner_assumption(self):
        path = Path(self.temp.name) / "pending-mute.json"
        path.write_text(json.dumps({"guild": 123, "user": 777}))
        reopened = bot.Speaker(self.temp.name)
        self.assertEqual(list(reopened.state(123).pending_users()), [777])
        self.assertEqual(reopened.state(123).member(777).journal.read()["user"], 777)
        self.assertFalse(path.exists())

    async def test_global_sync_and_legacy_cleanup(self):
        client = bot.Speaker(self.temp.name, legacy_guild_id=123)
        with patch.object(client.tree, "sync", new=AsyncMock()) as sync:
            await client.setup_hook()
        self.assertEqual(sync.await_args_list[0].kwargs, {})
        self.assertEqual(sync.await_args_list[1].kwargs["guild"].id, 123)

    async def test_commands_are_server_installed_and_access_is_manager_only(self):
        with patch.object(self.client.tree, "sync", new=AsyncMock()):
            await self.client.setup_hook()
        for command in self.client.tree.get_commands():
            payload = command.to_dict(self.client.tree)
            self.assertEqual(payload["contexts"], [0])
            self.assertEqual(payload["integration_types"], [0])
        policy = bot.access.to_dict(self.client.tree)
        self.assertEqual(policy["default_member_permissions"], 32)

    async def test_speech_mutes_and_restores_the_actual_caller(self):
        interaction, connection = self.voice_interaction(user_id=777)
        source = SimpleNamespace(cleanup=Mock())
        with patch.object(bot.edge_tts, "Communicate", return_value=SimpleNamespace(save=AsyncMock())), \
             patch.object(bot.discord.FFmpegOpusAudio, "from_probe", new=AsyncMock(return_value=source)), \
             patch.object(bot, "play_audio", new=AsyncMock()) as play:
            await bot.say.callback(interaction, text="Привет")
        play.assert_awaited_once_with(connection, source)
        self.assertEqual([call.kwargs["mute"] for call in interaction.user.edit.await_args_list], [True, False])
        self.assertIsNone(self.client.state(123).active_user_id)

    async def test_access_is_checked_again_after_synthesis(self):
        interaction, connection = self.voice_interaction()
        self.client.state(123).settings.set_role(789)
        interaction.user.roles = [SimpleNamespace(id=789)]
        fresh = SimpleNamespace(**vars(interaction.user))
        fresh.roles = []
        interaction.guild.fetch_member.return_value = fresh
        with patch.object(bot.edge_tts, "Communicate", return_value=SimpleNamespace(save=AsyncMock())):
            await bot.say.callback(interaction, text="Привет")
        interaction.user.voice.channel.connect.assert_not_awaited()
        interaction.user.edit.assert_not_awaited()

    async def test_two_servers_can_speak_independently(self):
        first, first_connection = self.voice_interaction(123)
        second, second_connection = self.voice_interaction(999)
        started, release = asyncio.Event(), asyncio.Event()
        async def play(connection, source):
            if connection is first_connection:
                started.set()
                await release.wait()
        source = SimpleNamespace(cleanup=Mock())
        with patch.object(bot.edge_tts, "Communicate", return_value=SimpleNamespace(save=AsyncMock())), \
             patch.object(bot.discord.FFmpegOpusAudio, "from_probe", new=AsyncMock(return_value=source)), \
             patch.object(bot, "play_audio", new=play):
            task = asyncio.create_task(bot.say.callback(first, text="Первая фраза"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(bot.say.callback(second, text="Вторая фраза"), 2)
                second_connection.disconnect.assert_awaited_once()
                self.assertTrue(self.client.state(123).lock.locked())
            finally:
                release.set()
                await task
