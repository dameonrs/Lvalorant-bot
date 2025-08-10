import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive

# --- デバッグユーティリティ ---
DEBUG = os.getenv("DEBUG_LOG") == "1"
def debug_log(*args):
    if DEBUG:
        print("[DEBUG]", *args, flush=True)
dlog = debug_log  # 呼び出し側の短縮名

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1394558478550433802

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- ランク定義 ---
RANK_FACTORS = {
    "アイアン": 0, "ブロンズ": 1, "シルバー": 2, "ゴールド": 3,
    "プラチナ": 4, "ダイヤモンド": 5, "アセンダント": 6,
    "イモータル": 7, "レディアント": 8
}

# --- ティア定義（各ティアを明示：直列 10..34） ---
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

# --- 状態 ---
party_sessions = OrderedDict()  # message_id -> {label, participants, start_time, reminded, next_posted}
party_labels = ['パーティA', 'パーティB', 'パーティC']
max_party_count = 3
latest_party_index = -1

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
        # --- 変更①: フルパ時はランク無制限、先頭5名が通常参加 ---
        for i, (uid, (name, r_str, r, t)) in enumerate(ordered):
            (temp_normals if i < 5 else temp_full).append((uid, name, r_str))
    else:
        # --- 変更②: 4人目(インデックス3)は必ず待機 ---
        for i, (uid, (name, r_str, r, t)) in enumerate(ordered):
            if i == 3:
                temp_full.append((uid, name, r_str))
                continue
            if i == 0 or (base_rank is not None and is_valid_by_base(r, t, base_rank, base_tier)):
                temp_normals.append((uid, name, r_str))
            else:
                temp_full.append((uid, name, r_str))

    dlog("temp_normals:", [(u, n) for u, n, _ in temp_normals],
         "temp_full:", [(u, n) for u, n, _ in temp_full])

    # --- (あなた) 表示を含む名前フォーマット関数 ---
    def format_name(uid, index, name, r_str, viewer_id):
        label = f"参加者{index + 1}"
        if viewer_id is not None and uid == viewer_id:
            label += " (あなた)"
        return f"- {label} ({r_str})"

    # 通常参加者（最大5人）
    normal = [
        format_name(uid, i, name, r_str, viewer_id)
        for i, (uid, name, r_str) in enumerate(temp_normals[:5])
    ]
    # 待機者（通常6人目以降 or 条件外）
    full = [
        format_name(uid, i + len(normal), name, r_str, viewer_id)
        for i, (uid, name, r_str) in enumerate(temp_full)
    ]

    channel = bot.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(message_id)
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

    await message.edit(embed=embed, view=JoinButtonView(message_id))

    if ended and not session.get("next_posted"):
        dlog("first party reached 5; next party posting trigger")
        session["next_posted"] = True
        if len(party_sessions) < max_party_count:
            await post_party_embed()
class JoinButtonView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="🎮 参加する", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = party_sessions[self.message_id]
        if session['label'] == 'パーティA' and datetime.datetime.now(pytz.timezone("Asia/Tokyo")) >= session['start_time']:
            await interaction.response.send_message("⚠️ 開始時間を過ぎているため、参加できません。", ephemeral=True)
            return

        if interaction.user.id in session['participants']:
            await interaction.response.send_message("✅ 既に参加済みです。", ephemeral=True)
        else:
            await interaction.response.send_message("🔽 ランクを選んでください：", view=RankSelectView(self.message_id), ephemeral=True)

    @discord.ui.button(label="❌ 取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = party_sessions[self.message_id]
        if session['label'] == 'パーティA' and datetime.datetime.now(pytz.timezone("Asia/Tokyo")) >= session['start_time']:
            await interaction.response.send_message("⚠️ 開始時間を過ぎているため、取り消しできません。", ephemeral=True)
            return

        if interaction.user.id in session['participants']:
            del session['participants'][interaction.user.id]
            await update_embed(self.message_id, interaction.user.id)
            await interaction.response.send_message("❌ 取り消しました。", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ まだ参加していません。", ephemeral=True)

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
        await update_embed(self.message_id, interaction.user.id)
        await interaction.followup.send(f"✅ ランク「**{rank_str}**」を登録しました！", ephemeral=True)

class RankSelectView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.add_item(RankSelect(message_id))

async def post_party_embed():
    global latest_party_index
    latest_party_index += 1
    label = party_labels[latest_party_index]
    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    start_time = now.replace(hour=21, minute=0, second=0, microsecond=0) if label == 'パーティA' else None

    channel = bot.get_channel(CHANNEL_ID)
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

    # 最初は message_id を渡せないので None で出す → 直後に update_embed で差し替え
    message = await channel.send(content='@everyone', embed=embed, view=JoinButtonView(None))

    party_sessions[message.id] = {
        "label": label,
        "participants": OrderedDict(),
        "start_time": start_time,
        "reminded": set(),
        "next_posted": False
    }
    await update_embed(message.id)

@tasks.loop(minutes=1)
async def daily_poster():
    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    if now.hour == 18 and now.minute == 45:
        party_sessions.clear()
        global latest_party_index
        latest_party_index = -1
        await post_party_embed()

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
            channel = bot.get_channel(CHANNEL_ID)
            mentions = [f"<@{uid}>" for uid in session['participants'] if uid not in session['reminded']]
            for uid in session['participants']:
                session['reminded'].add(uid)
            if mentions:
                await channel.send(f"🔔 {', '.join(mentions)} ゲーム開始まであと5分です！")

@bot.event
async def on_ready():
    print(f"✅ Bot is online: {bot.user}")
    if not daily_poster.is_running():
        daily_poster.start()
    if not reminder_task.is_running():
        reminder_task.start()

keep_alive()
bot.run(TOKEN)
