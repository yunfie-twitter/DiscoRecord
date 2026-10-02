from __future__ import annotations

import asyncio
import ctypes.util
import io
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import discord
    from discord.voice import VoiceClient
    from dotenv import load_dotenv
except (ImportError, OSError) as exc:
    raise SystemExit("Pycord または音声依存関係を読み込めません。requirements.txt をインストールしてください。"
                     f"（{type(exc).__name__}）") from None

# 録音機能を使う際は利用者に通知し、同意を得ることを推奨
# This file targets the exact Pycord revision in requirements.txt.
log = logging.getLogger("recorder")
intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Bot(intents=intents, allowed_mentions=discord.AllowedMentions.none())
rec = discord.SlashCommandGroup("rec", "ボイスチャンネルの録音", guild_only=True)


class RecordingSink(discord.sinks.MP3Sink):
    """MP3Sink with per-user conversion errors and a cleanup completion signal."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.ready = asyncio.Event()
        self.errors: dict[object, str] = {}

    def format_audio(self, audio):
        # Upstream MP3Sink does not check FFmpeg's return code. Check it here.
        original = audio.file
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le",
             "-ar", "48000", "-ac", "2", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
            input=original.read(), capture_output=True, check=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not result.stdout:
            raise RuntimeError("FFmpeg returned empty audio")
        audio.on_format(self.encoding)
        audio.file = io.BytesIO(result.stdout)
        original.close()

    def cleanup(self):
        # Upstream invokes its callback BEFORE cleanup. ready prevents early upload.
        try:
            self.finished = True
            for user, audio in list(self.audio_data.items()):
                try:
                    audio.cleanup()
                    self.format_audio(audio)
                except Exception as exc:
                    self.errors[user] = type(exc).__name__
                    log.exception("MP3 conversion failed")
        finally:
            self.loop.call_soon_threadsafe(self.ready.set)


@dataclass
class Session:
    guild: discord.Guild
    channel: discord.TextChannel | discord.Thread
    sink: RecordingSink
    phase: str = "starting"
    voice: VoiceClient | None = None
    message: discord.Message | None = None
    task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    failure: str | None = None


sessions: dict[int, Session] = {}
locks: dict[int, asyncio.Lock] = {}


def lock_for(guild_id: int) -> asyncio.Lock:
    return locks.setdefault(guild_id, asyncio.Lock())


def filename(user_id: int, name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")[:80]
    return f"{user_id}_{safe or 'unknown'}.mp3"


async def status(session: Session, text: str):
    if session.message:
        try:
            await session.message.edit(content=text)
        except discord.HTTPException:
            log.exception("Status message edit failed")


async def disconnect(session: Session):
    if session.voice:
        try:
            if session.voice.is_recording():
                await asyncio.to_thread(session.voice.stop_recording)
            await session.voice.disconnect(force=True)
        except Exception:
            log.exception("Voice disconnect failed")


def schedule_finish(session: Session):
    if session.task is None:
        session.task = asyncio.create_task(finish(session))


def recording_done(sink: RecordingSink, session: Session):
    # This callback can run on the receiver thread; never call create_task there.
    reader = getattr(session.voice, "_reader", None)
    if getattr(reader, "error", None):
        session.failure = "音声受信でエラーが発生しました。"
    sink.loop.call_soon_threadsafe(schedule_finish, session)


async def finish(session: Session):
    async with lock_for(session.guild.id):
        session.phase = "finishing"
    await status(session, "⏳ 録音終了処理中...")
    sent = failed = 0
    try:
        await session.sink.ready.wait()
        await disconnect(session)
        for user, audio in list(session.sink.audio_data.items()):
            if user in session.sink.errors:
                failed += 1
                continue
            user_id = getattr(user, "id", user if isinstance(user, int) else None)
            if user_id is None:
                failed += 1
                continue
            member = session.guild.get_member(user_id) or bot.get_user(user_id)
            name = getattr(user, "name", None) or getattr(member, "name", "unknown")
            audio.file.seek(0, io.SEEK_END)
            size = audio.file.tell()
            if size == 0 or size > session.guild.filesize_limit:
                failed += 1
                continue
            audio.file.seek(0)
            attachment = discord.File(audio.file, filename=filename(user_id, name))
            try:
                await session.channel.send(file=attachment)
                sent += 1
            except discord.HTTPException:
                failed += 1
                log.exception("Audio upload failed for user %s", user_id)
            finally:
                attachment.close()
        text = f"✅ 録音終了。{sent} 人分の MP3 を送信しました。"
        if not session.sink.audio_data:
            text = "⚠️ 録音終了。音声を取得できませんでした（無音・音声受信設定を確認）。"
        if failed:
            text += f"\n⚠️ {failed} 人分は変換失敗・容量超過・送信失敗などで送信できませんでした。"
        if session.failure:
            text += f"\n⚠️ {session.failure}"
        await status(session, text)
    except Exception:
        log.exception("Recording finalization failed")
        await status(session, "⚠️ 録音終了処理に失敗しました。Bot のログを確認してください。")
    finally:
        await disconnect(session)
        for audio in session.sink.audio_data.values():
            audio.file.close()
        session.sink.audio_data.clear()
        if session.watchdog and session.watchdog is not asyncio.current_task():
            session.watchdog.cancel()
        async with lock_for(session.guild.id):
            if sessions.get(session.guild.id) is session:
                sessions.pop(session.guild.id)


async def watch(session: Session):
    while sessions.get(session.guild.id) is session and session.phase == "recording":
        await asyncio.sleep(1)
        if session.phase != "recording":
            return
        if not session.voice.is_connected() or not session.voice.is_recording():
            session.failure = "ボイス接続または録音が予期せず終了しました。"
            session.phase = "finishing"
            # Disconnect normally stops the reader. Stop again only if still active.
            try:
                if session.voice.is_recording():
                    await asyncio.to_thread(session.voice.stop_recording)
            except Exception:
                log.exception("Automatic recording stop failed")
                await disconnect(session)
            finally:
                schedule_finish(session)
            return


@rec.command(name="start", description="録音を開始します")
async def start(ctx: discord.ApplicationContext,
                channel: discord.Option(discord.VoiceChannel, "録音先（省略時は参加先）", required=False) = None):
    await ctx.defer(ephemeral=True)
    guild = ctx.guild
    target = channel or getattr(getattr(ctx.author, "voice", None), "channel", None)
    if not isinstance(target, discord.VoiceChannel):
        await ctx.respond("通常のボイスチャンネルに参加するか、channel を指定してください。")
        return
    if not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
        await ctx.respond("サーバーのテキストチャンネルで実行してください。")
        return
    voice_permissions = target.permissions_for(guild.me)
    text_permissions = ctx.channel.permissions_for(guild.me)
    missing = [label for permission, label in
               [(voice_permissions.view_channel, "録音先の閲覧"),
                (voice_permissions.connect, "接続"), (voice_permissions.speak, "発言"),
                (text_permissions.view_channel, "送信先の閲覧"),
                (text_permissions.send_messages_in_threads if isinstance(ctx.channel, discord.Thread)
                 else text_permissions.send_messages, "メッセージ送信"),
                (text_permissions.attach_files, "ファイル添付")] if not permission]
    if missing:
        await ctx.respond("Bot の権限が不足しています：" + "、".join(missing))
        return
    async with lock_for(guild.id):
        if guild.id in sessions:
            await ctx.respond("既に録音中、または開始・終了処理中です。")
            return
        session = Session(guild, ctx.channel, RecordingSink(asyncio.get_running_loop()))
        sessions[guild.id] = session
        try:
            vc = guild.voice_client
            if vc and vc.is_recording():
                raise RuntimeError("Existing voice client is recording")
            if vc and vc.is_connected():
                session.voice = vc
                if vc.channel != target:
                    await vc.move_to(target)
            else:
                if vc:
                    await vc.disconnect(force=True)
                session.voice = await target.connect(timeout=30, self_deaf=False)
            await session.voice.guild.change_voice_state(channel=target, self_deaf=False)
            session.message = await ctx.channel.send("🔴 録音中...")
            # Nonempty args are necessary for this revision's callback invocation.
            session.voice.start_recording(session.sink, recording_done, session)
            session.phase = "recording"
            session.watchdog = asyncio.create_task(watch(session))
        except Exception:
            log.exception("Recording start failed")
            session.failure = "録音開始に失敗しました。権限・音声依存関係・接続を確認してください。"
            if session.voice and session.voice.is_recording():
                await asyncio.to_thread(session.voice.stop_recording)
                schedule_finish(session)
            else:
                await disconnect(session)
                await status(session, "⚠️ " + session.failure)
                sessions.pop(guild.id, None)
            await ctx.respond(session.failure)
            return
    await ctx.respond("録音を開始しました。終了するには /rec stop を実行してください。")


@rec.command(name="stop", description="録音を停止して MP3 を送信します")
async def stop(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    async with lock_for(ctx.guild.id):
        session = sessions.get(ctx.guild.id)
        if session is None:
            await ctx.respond("このサーバーでは録音していません。")
            return
        if session.phase != "recording":
            await ctx.respond("録音の開始・終了処理中です。")
            return
        session.phase = "finishing"
    await status(session, "⏳ 録音終了処理中...")
    try:
        await ctx.respond("録音を停止しています。MP3 は録音開始時のチャンネルに送信します。")
    except discord.HTTPException:
        log.exception("Stop acknowledgement failed; continuing to stop")
    try:
        await asyncio.to_thread(session.voice.stop_recording)
        schedule_finish(session)
    except Exception:
        log.exception("Recording stop failed")
        session.failure = "録音停止に失敗しました。"
        await disconnect(session)
        # Disconnect triggers reader cleanup if it is still active.
        if not session.sink.ready.is_set():
            # A missing reader cannot trigger cleanup; discard unformatted audio.
            for user in session.sink.audio_data:
                session.sink.errors[user] = "stop failed"
            session.sink.ready.set()
        schedule_finish(session)


@bot.event
async def on_application_command_error(ctx, error):
    log.error("Command failed", exc_info=(type(error), error, error.__traceback__))
    try:
        await ctx.respond("コマンド処理に失敗しました。Bot のログと権限を確認してください。", ephemeral=True)
    except discord.HTTPException:
        log.exception("Error response failed")


bot.add_application_command(rec)


def check_runtime():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg が見つかりません。インストールして PATH に追加してください。")
    import davey  # noqa: F401 -- fail early when voice dependencies are missing
    import nacl  # noqa: F401
    if not discord.opus.is_loaded():
        library = os.getenv("OPUS_LIBRARY") or ctypes.util.find_library("opus")
        if library:
            discord.opus.load_opus(library)
        else:
            # On Windows Pycord loads its bundled Opus DLL here.
            discord.opus._load_default()
    if not discord.opus.is_loaded():
        raise RuntimeError("Opus を読み込めません。libopus または OPUS_LIBRARY を確認してください。")
    discord.opus.Decoder()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Read only the .env next to main.py; externally set variables take priority.
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False, encoding="utf-8-sig")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit(".env または環境変数に DISCORD_TOKEN を設定してください。")
    try:
        check_runtime()
    except Exception as exc:
        raise SystemExit(f"起動前チェックに失敗しました：{type(exc).__name__}: {exc}") from None
    bot.run(token)
