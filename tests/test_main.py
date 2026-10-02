import asyncio
import io
import math
import os
import shutil
import struct
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import main


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.sessions.clear()
        main.locks.clear()

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
