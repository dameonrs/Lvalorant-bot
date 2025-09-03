# ==== imports ====
import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive
import asyncio, time
from collections import defaultdict
import sys, logging, re, random

# ==== 連打ガード（現行維持）====
_last_click = defaultdict(dict)  # message_id -> {user_id: timestamp}

def rapid_click(message_id: int, user_id: int, window: float = 0.8) -> bool:
    """同一ユーザーの0.8秒以内の多重押下を無視"""
    now = time.monotonic()
    last = _last_click[message_id].get(user_id, 0.0)
    if now - last < window:
        return True
    _last_click[message_id][user_id] = now
    return False

# ==== followup の軽いリトライ（現行維持＋1015文言も拾う）====
async def safe_followup_send(interaction: discord.Interaction, content: str = None, *, view=None, ephemeral: bool = True):
    """Cloudflare 1015/Discord 429 対策：軽いバックオフで最大3回再試行"""
    for attempt in range(3):
        try:
            return await interaction.followup.send(content, view=view, ephemeral=ephemeral)
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429 or "1015" in str(e):
                await asyncio.sleep(1.0 * (attempt + 1))  # 1s, 2s, 3s
                continue
            raise

# ==== 共通APIラッパ + セマフォ（新規）====
_api_sem = asyncio.Semaphore(2)  # 同時API実行本数を制限（2〜3で調整可）

async def safe_api_call(coro_func, *args, retries=5, **kwargs):
    """
    Discord API 呼び出しの共通ラッパ。
    429（Discord）/1015（Cloudflare）/502/503/一部403 を指数バックオフで再試行。
    """
    backoff = 0.8
    for attempt in range(retries):
        async with _api_sem:
            try:
                return await coro_func(*args, **kwargs)
            except discord.HTTPException as e:
                status = getattr(e, "status", None)
                msg = str(e)
                if status in (429, 502, 503) or "1015" in msg or (status == 403 and "@everyone" in msg):
                    if attempt == retries - 1:
                        raise
                    await asyncio.sleep(backoff + random.random() * 0.5)
                    backoff *= 1.6
                    continue
                raise

# ==== 環境変数の堅牢パース（新規）====
def _parse_channel_id(raw):
    if not raw:
        print("❌ CHANNEL_ID env is missing")
        return None
    s = str(raw).strip().strip('"').strip("'")
    s_digits = re.sub(r"\D", "", s)
    if not s_digits:
        print(f"❌ CHANNEL_ID not numeric: raw={repr(raw)} / cleaned={repr(s)}")
        return None
    try:
        return int(s_digits)
    except Exception as e:
        print(f"❌ CHANNEL_ID int() failed: {e} / cleaned={repr(s_digits)}")
        return None

# ==== TOKEN/CHANNEL_ID 取得（起動前チェック強化）====
TOKEN = os.getenv("DISCORD_TOKEN")
raw_ch = os.getenv("CHANNEL_ID")
print("[BOOT] TOKEN set? ->", bool(TOKEN), "| CHANNEL_ID raw ->", repr(raw_ch))
CHANNEL_ID = _parse_channel_id(raw_ch)

if not TOKEN:
    print("❌ DISCORD_TOKEN is missing. Set environment variable DISCORD_TOKEN.")
if not CHANNEL_ID:
    print("❌ CHANNEL_ID is invalid/missing. Set environment variable CHANNEL_ID (numeric).")

if not TOKEN or not CHANNEL_ID:
    raise SystemExit("Startup aborted due to invalid/missing env vars.")

# ==== ログ設定の互換化（setup_logging -> fallback）====
try:
    discord.utils.setup_logging(level=logging.INFO)
except Exception:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("discord").setLevel(logging.INFO)
    print("ℹ️ setup_logging not available; fell back to logging.basicConfig")

# --- デバッグユーティリティ（現行維持） ---
DEBUG = os.getenv("DEBUG_LOG") == "1"
def debug_log(*args):
    if DEBUG:
        print("[DEBUG]", *args, flush=True)
dlog = debug_log  # 呼び出し側の短縮名

# ==== intents/bot（現行維持）====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- ランク定義（現行維持） ---
RANK_FACTORS = {
    "アイアン": 0, "ブロンズ": 1, "シルバー": 2, "ゴールド": 3,
    "プラチナ": 4, "ダイヤモンド": 5, "アセンダント": 6,
    "イモータル": 7, "レディアント": 8
}

# --- ティア定義（現行維持） ---
TIER_MAP = {
    "アイアン1": 10, "アイアン2": 11, "アイアン3": 12,
    "ブロンズ1": 13, "ブロンズ2": 14, "ブロンズ3": 15,
    "シルバー1": 16, "シルバー2": 17, "シルバー3": 18,
    "ゴールド1": 19, "ゴールド2": 20, "ゴールド3": 21,
    "プラチナ1": 22, "プラチナ2": 23, "プラチナ3": 24,
    "ダイヤモンド1": 25, "ダイヤモンド2": 26, "ダイヤモンド3": 27,
    "アセンダント1": 28, "アセンダント2": 29, "アセンダント3": 30,
    "イモータル1": 31, "イモータル2": 32, "イモータル3": 33,
    "レディアント": 34
}

# --- 状態（現行維持） ---
party_sessions = OrderedDict()  # message_id -> {label, participants, start_time, reminded, next_posted}
party_labels = ['パーティA', 'パーティB', 'パーティC']
max_party_count = 3
latest_party_index = -1

# ==== チャンネル取得のガード（新規）====
async def _get_channel():
    ch = bot.get_channel(CHANNEL_ID)
    if ch is not None:
        return ch
    try:
        ch = await safe_api_call(bot.fetch_channel, CHANNEL_ID)
        return ch
    except Exception as e:
        print(f"❌ fetch_channel失敗: {e} / CHANNEL_ID={CHANNEL_ID}")
        return None

# --- 判定（現行維持） ---
def is_valid_by_base(new_rank, new_tier, base_rank, base_tier):
    """
    マッチング仕様：
      ① プラチナ以下同士 → 前後1ランク（ティア無視）
      ② 片方でもダイヤ以上 → 直列ティア差 ±3（TIER_MAP 値で比較）
    ※ フルパは別処理で常に無制限
    """
    PLAT = RANK_FACTORS["プラチナ"]
    if new_rank is None or base_rank is None or new_tier is None or base_tier is None:
        return False
    if new_rank <= PLAT and base_rank <= PLAT:
        return abs(new_rank - base_rank) <= 1
    return abs(int(new_tier) - int(base_tier)) <= 3

def get_base_participant(participants):
    for _, (_, rank_str, rank, tier) in participants.items():
        return rank_str, rank, tier
    return "未設定", None, None

# ==== update_embed（API呼び出しを safe_api_call 化／CHガード適用）====
async def update_embed(message_id, viewer_id=None):
    session = party_sessions[message_id]
    participants = session["participants"]
    base_rank_str, base_rank, base_tier = get_base_participant(participants)

    dlog(f"update_embed called: message_id={message_id}, viewer_id={viewer_id}")
    dlog(f"participants order: {list(participants.keys())}")

    temp_normals = []
    temp_full = []

    ordered = list(participants.items())  # [(uid, (name, r_str, r, t)), ...]
    count = len(ordered)

    if count >= 5:
        # --- 現行仕様維持：フルパ時はランク無制限、先頭5名が通常参加 ---
        for i, (uid, (name, r_str, r, t)) in enumerate(ordered):
            (temp_normals if i < 5 else temp_full).append((uid, name, r_str))
    else:
        # --- 現行仕様維持：'基準内で通常枠に入れた人数'で制御（基準含め最大3） ---
        normals_count = 0  # 先頭(基準)を含めた「基準内の通常枠」人数

        for i, (uid, (name, r_str, r, t)) in enumerate(ordered):
            if i == 0:
                temp_normals.append((uid, name, r_str))
                normals_count = 1
                continue

            is_valid = (base_rank is not None) and is_valid_by_base(r, t, base_rank, base_tier)

            if is_valid and normals_count < 3:
                temp_normals.append((uid, name, r_str))
                normals_count += 1
            else:
                temp_full.append((uid, name, r_str))

    dlog("temp_normals:", [(u, n) for u, n, _ in temp_normals],
         "temp_full:", [(u, n) for u, n, _ in temp_full])

    # 公開用（現行仕様維持：実名＋行にランク表示）
    def format_name(uid, index, name, r_str, viewer_id):
        return f"- 参加者{index + 1}：{name}（{r_str}）"

    normal = [format_name(uid, i, name, r_str, viewer_id) for i, (uid, name, r_str) in enumerate(temp_normals[:5])]
    full = [format_name(uid, i + len(normal), name, r_str, viewer_id) for i, (uid, name, r_str) in enumerate(temp_full)]

    channel = await _get_channel()
    if channel is None:
        print("❌ update_embed: チャンネル取得失敗のため中止")
        return

    message = await safe_api_call(channel.fetch_message, message_id)
    embed = message.embeds[0]

    is_first_party = session['label'] == 'パーティA'
    ended = len(participants) >= 5 and is_first_party

    embed.title = f"🎮 VALORANT {session['label']}{' 🔒 募集終了' if ended else ''}"
    embed.description = (
        (f"🕒 開始時刻：21:00\n" if is_first_party else "")
        + f"基準ランク：{base_rank_str}　フルパ：無制限\n\n"
        + ("**🟢 通常参加者（条件内・最大5人）**\n" + ("\n".join(normal) if normal else "（なし）"))
        + "\n\n**🔴 フルパ待機者（条件外または6人目以降）**\n"
        + (("\n".join(full)) if full else "（なし）")
    )

    await safe_api_call(message.edit, embed=embed, view=JoinButtonView(message_id))

    if ended and not session.get("next_posted"):
        dlog("first party reached 5; next party posting trigger")
        session["next_posted"] = True
        if len(party_sessions) < max_party_count:
            await post_party_embed()

# ここからは「関数の外」（インデント0）現行仕様維持のビュー
def make_personal_join_view(message_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="🎮 参加する", style=discord.ButtonStyle.primary, disabled=True))
    v.add_item(RankSelect(message_id))
    return v

def make_personal_cancel_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="❌ 取り消す", style=discord.ButtonStyle.danger, disabled=True))
    return v

class JoinButtonView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="🎮 参加する", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 初回応答は defer（タイムアウト&連打を避ける）
        await interaction.response.defer(ephemeral=True)

        # 擬似連打ガード（0.8秒）
        if rapid_click(self.message_id, interaction.user.id):
            return await safe_followup_send(
                interaction, "処理中です…少し待ってください。", view=make_personal_join_view(self.message_id)
            )

        session = party_sessions[self.message_id]
        jst = pytz.timezone("Asia/Tokyo")
        if session['label'] == 'パーティA' and datetime.datetime.now(jst) >= session['start_time']:
            return await safe_followup_send(
                interaction, "⚠️ 開始時間を過ぎているため、参加できません。", view=make_personal_join_view(self.message_id)
            )

        if interaction.user.id in session['participants']:
            return await safe_followup_send(
                interaction, "✅ 既に参加済みです。", view=make_personal_join_view(self.message_id)
            )
        else:
            # 本人だけ：Joinグレーアウト＋ランクセレクト
            return await safe_followup_send(
                interaction, "🔽 ランクを選んでください：", view=make_personal_join_view(self.message_id)
            )

    @discord.ui.button(label="❌ 取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 初回応答は defer
        await interaction.response.defer(ephemeral=True)

        # 擬似連打ガード
        if rapid_click(self.message_id, interaction.user.id):
            return await safe_followup_send(
                interaction, "処理中です…少し待ってください。", view=make_personal_cancel_view()
            )

        session = party_sessions[self.message_id]
        jst = pytz.timezone("Asia/Tokyo")
        if session['label'] == 'パーティA' and datetime.datetime.now(jst) >= session['start_time']:
            return await safe_followup_send(
                interaction, "⚠️ 開始時間を過ぎているため、取り消しできません。", view=make_personal_cancel_view()
            )

        if interaction.user.id in session['participants']:
            del session['participants'][interaction.user.id]
            # ← 即時 update はレート制限の原因になるので安全化：デバウンス実装を適用
            schedule_update(self.message_id, interaction.user.id)
            return await safe_followup_send(
                interaction, "❌ 取り消しました。", view=make_personal_cancel_view()
            )
        else:
            return await safe_followup_send(
                interaction, "⚠️ まだ参加していません。", view=make_personal_cancel_view()
            )

class RankSelect(discord.ui.Select):
    def __init__(self, message_id):
        options = [discord.SelectOption(label=rank) for rank in TIER_MAP.keys()]
        super().__init__(placeholder="ランクを選んでください", min_values=1, max_values=1, options=options)
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rank_str = self.values[0]
        tier = TIER_MAP[rank_str]
        base = rank_str.rstrip("123")
        rank = RANK_FACTORS.get(base)
        if rank is None:
            await interaction.followup.send("⚠️ ランク解析に失敗しました。", ephemeral=True)
            return

        session = party_sessions[self.message_id]
        session['participants'][interaction.user.id] = (interaction.user.display_name, rank_str, rank, tier)
        # ← 即時 update はレート制限の原因になるので安全化：デバウンス実装を適用
        schedule_update(self.message_id, interaction.user.id)
        await interaction.followup.send(f"✅ ランク「**{rank_str}**」を登録しました！", ephemeral=True)

class RankSelectView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.add_item(RankSelect(message_id))

# ==== update_embed のデバウンス（新規）====
_pending_updates = {}  # message_id -> asyncio.Task

def schedule_update(message_id, viewer_id=None):
    """短時間の多重更新をまとめて1回にする（0.6s待って最後だけ実行）"""
    existing = _pending_updates.get(message_id)
    if existing and not existing.done():
        return  # すでに待機中なら新規に積まない

    async def _runner():
        try:
            await asyncio.sleep(0.6)
            await update_embed(message_id, viewer_id)
        finally:
            _pending_updates.pop(message_id, None)

    _pending_updates[message_id] = asyncio.create_task(_runner())

# ==== 投稿（API安全化・CHガード・@everyone安全化）====
async def post_party_embed():
    global latest_party_index
    latest_party_index += 1
    label = party_labels[latest_party_index]
    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    start_time = now.replace(hour=21, minute=0, second=0, microsecond=0) if label == 'パーティA' else None

    channel = await _get_channel()
    if channel is None:
        print("❌ post_party_embed: チャンネル取得失敗のため中止")
        return

    embed = discord.Embed(
        title=f"🎮 VALORANT {label}",
        description=(
            "🕒 基準ランク：未設定　時間設定：アナウンスしてください　フルパ：無制限\n\n"
            "**🟢 通常参加者（条件内・最大5人）**\n（なし）\n\n"
            "**🔴 フルパ待機者（条件外または6人目以降）**\n（なし）"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="参加希望の方は下のボタンをクリックしてください")

    try:
        message = await safe_api_call(
            channel.send,
            content='@everyone',
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False)  # 403回避（本当に通知したい時は外す）
        )
    except discord.HTTPException as e:
        print(f"❌ 投稿失敗 HTTP {getattr(e,'status',None)}: {e}")
        return

    # セッション登録（現行維持）
    party_sessions[message.id] = {
        "label": label,
        "participants": OrderedDict(),
        "start_time": start_time,
        "reminded": set(),
        "next_posted": False
    }

    # 初回は即時に埋め込み更新（ここは即時でOK）
    await update_embed(message.id)

# ==== 自動投稿（現行の18:40維持）====
@tasks.loop(minutes=1)
async def daily_poster():
    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    if now.hour == 18 and now.minute == 40:  # ← 現行仕様を維持
        party_sessions.clear()
        global latest_party_index
        latest_party_index = -1
        await post_party_embed()

# ==== 開始5分前リマインダ（API安全化＆CHガード適用）====
@tasks.loop(minutes=1)
async def reminder_task():
    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    for session in party_sessions.values():
        if session['label'] != 'パーティA':
            continue
        if session['start_time'] is None:
            continue
        delta = (session['start_time'] - now).total_seconds()
        if 0 < delta <= 300:
            channel = await _get_channel()
            if channel is None:
                print("❌ reminder: チャンネル取得に失敗")
                continue
            mentions = [f"<@{uid}>" for uid in session['participants'] if uid not in session['reminded']]
            for uid in session['participants']:
                session['reminded'].add(uid)
            if mentions:
                await safe_api_call(channel.send, f"🔔 {', '.join(mentions)} ゲーム開始まであと5分です！")

# ==== 起動時 ====
@bot.event
async def on_ready():
    print(f"✅ Bot is online: {bot.user} (discord.py {discord.__version__})")
    ch = await _get_channel()
    if ch is None:
        print(f"❌ Channel not found (ID={CHANNEL_ID}). Guild参加/権限/IDの確認を！")
        return  # 致命的なのでこのままタスク開始しない

    if not daily_poster.is_running():
        daily_poster.start()
    if not reminder_task.is_running():
        reminder_task.start()

# ==== 実行 ====
keep_alive()
try:
    bot.run(TOKEN)
except discord.errors.PrivilegedIntentsRequired as e:
    print("❌ PrivilegedIntentsRequired: Developer Portal で MESSAGE CONTENT INTENT を有効にしてください。")
    raise
except Exception as e:
    print(f"❌ bot.run() failed: {type(e).__name__}: {e}")
    raise
