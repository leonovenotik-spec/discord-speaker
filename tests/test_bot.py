import asyncio
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("speaker", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class MuteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = bot.MuteJournal(self.temp.name)
        self.guild = SimpleNamespace(id=123, owner_id=456)
        self.member = SimpleNamespace(id=456, guild=self.guild,
            voice=SimpleNamespace(channel=object(), mute=False), edit=AsyncMock())
        self.guild.fetch_member = AsyncMock(return_value=self.member)

    async def test_restores_after_success(self):
        async with bot.muted_for_speech(self.member, self.journal):
            self.assertTrue(self.journal.path.exists())
        self.assertEqual([c.kwargs["mute"] for c in self.member.edit.await_args_list], [True, False])
        self.assertIsNone(self.journal.read())

    async def test_restores_after_playback_failure(self):
        with self.assertRaisesRegex(RuntimeError, "playback"):
            async with bot.muted_for_speech(self.member, self.journal):
                raise RuntimeError("playback")
        self.assertEqual(self.member.edit.await_args_list[-1].kwargs["mute"], False)
        self.assertIsNone(self.journal.read())

    async def test_never_removes_existing_mute(self):
        self.member.voice.mute = True
        with self.assertRaises(bot.UserError):
            async with bot.muted_for_speech(self.member, self.journal):
                self.fail("Must not play")
        self.member.edit.assert_not_awaited()
        self.assertIsNone(self.journal.read())

    async def test_recovery_survives_restart(self):
        self.journal.record(self.member)
        reopened = bot.MuteJournal(self.temp.name)
        await reopened.restore(self.guild)
        self.member.edit.assert_awaited_once()
        self.assertIsNone(reopened.read())

    async def test_disconnected_member_keeps_recovery(self):
        self.journal.record(self.member)
        self.member.voice = None
        with self.assertRaises(bot.UserError):
            await self.journal.restore(self.guild)
        self.assertIsNotNone(self.journal.read())
        self.member.edit.assert_not_awaited()

    async def test_changed_owner_is_not_unmuted(self):
        self.journal.record(self.member)
        self.guild.owner_id = 999
        with self.assertRaises(bot.UserError):
            await self.journal.restore(self.guild)
        self.member.edit.assert_not_awaited()

    async def test_failed_restore_keeps_journal(self):
        self.journal.record(self.member)
        self.member.edit.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            await self.journal.restore(self.guild)
        self.assertIsNotNone(self.journal.read())

    async def test_command_registration(self):
        client = bot.Speaker(123, self.temp.name)
        with patch.object(client.tree, "sync", new=AsyncMock()):
            await client.setup_hook()
        self.assertEqual({x.name for x in client.tree.get_commands()}, {"say", "stop", "restore", "panel"})

    async def test_preferences_survive_restart(self):
        prefs = bot.Preferences(self.temp.name)
        prefs.set(voice="en-US-EmmaMultilingualNeural", speed="+30%")
        reopened = bot.Preferences(self.temp.name)
        self.assertEqual(reopened.voice, "en-US-EmmaMultilingualNeural")
        self.assertEqual(reopened.speed, "+30%")

    async def test_invalid_preferences_do_not_replace_saved_choices(self):
        prefs = bot.Preferences(self.temp.name)
        prefs.set(speed="-15%")
        with self.assertRaises(bot.UserError):
            prefs.set(voice="unknown", speed="+30%")
        self.assertEqual(bot.Preferences(self.temp.name).speed, "-15%")

    async def test_panel_shows_saved_voice_and_speed(self):
        client = bot.Speaker(123, self.temp.name)
        client.preferences.set(voice="en-US-AvaMultilingualNeural", speed="-30%")
        view = bot.ControlPanel(client)
        menus = [item for item in view.children if isinstance(item, bot.PreferenceSelect)]
        selected = {menu.kind: [option.value for option in menu.options if option.default] for menu in menus}
        self.assertEqual(selected, {"voice": ["en-US-AvaMultilingualNeural"], "speed": ["-30%"]})
        self.assertEqual(len(view.to_components()), 3)
        view.stop()

    async def test_modal_uses_the_same_speech_handler(self):
        modal = bot.SpeechModal()
        modal.text._value = "Проверка кнопки"
        handler = AsyncMock()
        interaction = object()
        with patch.object(bot, "say", SimpleNamespace(callback=handler)):
            await modal.on_submit(interaction)
        handler.assert_awaited_once_with(interaction, text="Проверка кнопки")

    async def test_panel_denies_other_members(self):
        client = bot.Speaker(123, self.temp.name)
        view = bot.ControlPanel(client)
        interaction = SimpleNamespace(client=client, guild=self.guild,
            user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()
        view.stop()


if __name__ == "__main__":
    unittest.main()
