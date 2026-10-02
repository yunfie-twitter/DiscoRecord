import asyncio
import io
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec, patch

import discord
import main


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.sessions.clear()
        main.locks.clear()
        self.record_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.record_directory.cleanup)
        self.record_patch = patch.object(main, "RECORD_DIR", Path(self.record_directory.name))
        self.record_patch.start()
        self.addCleanup(self.record_patch.stop)

    def session(self, guild_id=1):
        guild = SimpleNamespace(id=guild_id, filesize_limit=1000, get_member=lambda _: None)
        channel = SimpleNamespace(send=AsyncMock())
        sink = main.RecordingSink(asyncio.get_running_loop())
        session = main.Session(guild, channel, sink, phase="recording")
        session.voice = SimpleNamespace(disconnect=AsyncMock(), is_recording=lambda: False)
        session.message = SimpleNamespace(edit=AsyncMock())
        main.sessions[guild_id] = session
        return session

    def add_audio(self, session, user_id, size=100):
        user = SimpleNamespace(id=user_id, name=f"user/{user_id}")
        # Use a hashable stand-in for discord.Member.
        user = type("User", (), {"id": user.id, "name": user.name})()
        audio = discord.sinks.AudioData(io.BytesIO(b"a" * size))
        session.sink.audio_data[user] = audio
        return user, audio.file

    async def test_waits_for_cleanup_and_clears_state(self):
        session = self.session()
        _, buffer = self.add_audio(session, 10)
        task = asyncio.create_task(main.finish(session))
        await asyncio.sleep(0.01)
        session.channel.send.assert_not_awaited()
        self.assertIn(1, main.sessions)
        session.sink.ready.set()
        with patch.object(main.bot, "get_user", return_value=None):
            await task
        self.assertEqual(session.channel.send.await_args.kwargs["file"].filename, "10_user_10.mp3")
        self.assertNotIn(1, main.sessions)
        self.assertTrue(buffer.closed)
        saved = list(Path(self.record_directory.name).rglob("*.mp3"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"a" * 100)

    async def test_upload_failure_size_and_conversion_error_continue(self):
        session = self.session()
        self.add_audio(session, 10)
        self.add_audio(session, 11)
        self.add_audio(session, 12, 1001)
        user, _ = self.add_audio(session, 13)
        session.sink.errors[user] = "conversion"
        session.channel.send.side_effect = [discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "denied"), None]
        session.sink.ready.set()
        with patch.object(main.bot, "get_user", return_value=None):
            await main.finish(session)
        self.assertEqual(session.channel.send.await_count, 2)
        self.assertIn("1 人分", session.message.edit.await_args.kwargs["content"])
        self.assertIn("3 人分", session.message.edit.await_args.kwargs["content"])
        self.assertFalse(main.sessions)
        self.assertEqual(len(list(Path(self.record_directory.name).rglob("*.mp3"))), 3)

    async def test_id_only_user_name_fetched_when_not_cached(self):
        session = self.session()
        session.sink.audio_data[10] = discord.sinks.AudioData(io.BytesIO(b"mp3"))
        session.sink.ready.set()
        with patch.object(main.bot, "get_user", return_value=None), patch.object(
            main.bot, "fetch_user", new=AsyncMock(return_value=SimpleNamespace(name="Alice"))
        ) as fetch:
            await main.finish(session)
        fetch.assert_awaited_once_with(10)
        self.assertEqual(session.channel.send.await_args.kwargs["file"].filename, "10_Alice.mp3")
        self.assertEqual(list(Path(self.record_directory.name).rglob("*.mp3"))[0].name, "10_Alice.mp3")

    async def test_snapshot_name_and_separate_recordings(self):
        for _ in range(2):
            session = self.session()
            session.names[10] = "Alice"
            session.sink.audio_data[10] = discord.sinks.AudioData(io.BytesIO(b"mp3"))
            session.sink.ready.set()
            with patch.object(main.bot, "get_user", return_value=None), patch.object(
                main.bot, "fetch_user", new=AsyncMock()
            ) as fetch:
                await main.finish(session)
            fetch.assert_not_awaited()
        saved = list(Path(self.record_directory.name).rglob("10_Alice.mp3"))
        self.assertEqual(len(saved), 2)
        self.assertNotEqual(saved[0].parent, saved[1].parent)

    async def test_save_failure_does_not_prevent_upload(self):
        session = self.session()
        self.add_audio(session, 10)
        session.sink.ready.set()
        with patch.object(main.bot, "get_user", return_value=None), patch.object(
            main, "save_audio", side_effect=PermissionError("denied")
        ):
            await main.finish(session)
        session.channel.send.assert_awaited_once()
        self.assertIn("保存に失敗", session.message.edit.await_args.kwargs["content"])
        self.assertFalse(main.sessions)

    async def test_empty_and_multiple_guilds(self):
        first, second = self.session(1), self.session(2)
        first.sink.ready.set()
        await main.finish(first)
        self.assertIn(2, main.sessions)
        self.assertIn("音声を取得できません", first.message.edit.await_args.kwargs["content"])
        second.sink.ready.set()
        await main.finish(second)

    async def test_stop_when_not_recording(self):
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1), defer=AsyncMock(), respond=AsyncMock())
        await main.stop.callback(ctx)
        self.assertIn("録音していません", ctx.respond.await_args.args[0])

    async def test_start_uses_installed_connect_signature(self):
        guild = SimpleNamespace(id=1, me=object(), voice_client=None,
                                change_voice_state=AsyncMock())
        target = Mock(spec=discord.VoiceChannel)
        target.permissions_for.return_value = discord.Permissions.all()
        voice = SimpleNamespace(guild=guild, start_recording=Mock())
        # Autospec checks the real installed API, rejecting unsupported kwargs.
        target.connect = create_autospec(discord.VoiceChannel.connect)
        target.connect.return_value = voice
        # Bound channel methods do not receive self from the command.
        async def connect(**kwargs):
            return await target.connect(target, **kwargs)
        bound_target = Mock(spec=discord.VoiceChannel)
        bound_target.members = []
        bound_target.permissions_for.return_value = discord.Permissions.all()
        bound_target.connect = connect
        channel = Mock(spec=discord.TextChannel)
        channel.permissions_for.return_value = discord.Permissions.all()
        channel.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
        ctx = SimpleNamespace(guild=guild, channel=channel, defer=AsyncMock(),
                              respond=AsyncMock(), author=SimpleNamespace())
        with patch.object(main, "watch", new=AsyncMock()):
            await main.start.callback(ctx, bound_target)
        target.connect.assert_awaited_once_with(target, timeout=30)
        guild.change_voice_state.assert_awaited_once_with(channel=bound_target, self_deaf=False)
        voice.start_recording.assert_called_once()
        self.assertEqual(main.sessions[1].phase, "recording")
        await main.sessions[1].watchdog

    async def test_stop_in_progress(self):
        session = self.session()
        session.phase = "finishing"
        ctx = SimpleNamespace(guild=session.guild, defer=AsyncMock(), respond=AsyncMock())
        await main.stop.callback(ctx)
        self.assertIn("処理中", ctx.respond.await_args.args[0])

    async def test_cleanup_isolates_conversion_errors(self):
        session = self.session()
        bad, _ = self.add_audio(session, 10)
        good, _ = self.add_audio(session, 11)
        with patch.object(session.sink, "format_audio", side_effect=[RuntimeError("bad"), None]):
            await asyncio.to_thread(session.sink.cleanup)
        await session.sink.ready.wait()
        self.assertIn(bad, session.sink.errors)
        self.assertNotIn(good, session.sink.errors)
        for audio in session.sink.audio_data.values():
            audio.file.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not on PATH")
    async def test_real_mp3_conversion(self):
        session = self.session()
        pcm = b"".join(struct.pack("<hh", value, value) for value in
                       (int(10000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(4800)))
        audio = discord.sinks.AudioData(io.BytesIO(pcm))
        session.sink.audio_data[10] = audio
        await asyncio.to_thread(session.sink.cleanup)
        await session.sink.ready.wait()
        self.assertFalse(session.sink.errors)
        encoded = audio.file.getvalue()
        result = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                                input=encoded, capture_output=True, check=True)
        self.assertGreater(len(result.stdout), 1000)
        audio.file.close()


if __name__ == "__main__":
    unittest.main()
