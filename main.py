import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive

# --- デバッグユーティリティ ---
def debug_log(*args):
    print("[DEBUG]", *args)

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

TIER_MAP = {
    f"{rank}{tier}": 10 + i for i, (rank, tiers) in enumerate([
        ("アイアン", 3), ("ブロンズ", 3), ("シルバー", 3), ("ゴールド", 3),
        ("プラチナ", 3), ("ダイヤモンド", 3), ("アセンダント", 3), ("イモータル", 3)
    ]) for tier in range(1, tiers + 1)
}
TIER_MAP["レディアント"] = 34

party_sessions = OrderedDict()
party_labels = ['パーティA', 'パーティB', 'パーティC']
max_party_count = 3
latest_party_index = -1

def is_valid_by_base(new_rank, new_tier, base_rank, base_tier):
    if new_tier >= 25 or base_tier >= 25:
        if abs(new_tier - base_tier) > 6:
            return False
        return base_tier - 3 <= new_tier <= base_tier + 3
    else:
        return abs(new_rank - base_rank) <= 1

def get_base_participant(participants):
    for _, (_, rank_str, rank, tier) in participants.items():
        return rank_str, rank, tier
    return "未設定", None, None

async def update_embed(message_id, viewer_id=None):
    session = party_sessions[message_id]
    participants = session["participants"]
    base_rank_str, base_rank, base_tier = get_base_participant(participants)

    # ▼ 追加: 呼び出しと参加者順のトレース
    dlog(f"update_embed called: message_id={message_id}, viewer_id={viewer_id}")
    dlog(f"participants order: {list(participants.keys())}")

    temp_normals = []
    temp_full = []
    for uid, (name, r_str, r, t) in participants.items():
        if uid == next(iter(participants)):
            temp_normals.append((uid, name, r_str))
        elif base_rank is not None and is_valid_by_base(r, t, base_rank, base_tier):
            temp_normals.append((uid, name, r_str))
        else:
            temp_full.append((uid, name, r_str))

    while len(temp_normals) < 5 and temp_full:
        temp_normals.append(temp_full.pop(0))

    # ▼ 追加: 振り分け結果のトレース
    dlog("temp_normals:", [(u, n) for u, n, _ in temp_normals], "temp_full:", [(u, n) for u, n, _ in temp_full])

    # ▼ 修正版：自分だけ名前表示、それ以外は「参加者N」
    def format_name(uid, index, name, r_str, viewer_id):
        is_you = (uid == viewer_id)
        dlog(f"format_name: uid={uid}, name={name}, idx={index}, rank={r_str}, viewer_id={viewer_id}, is_you={is_you}")
        label = f"{name}（あなた）" if is_you else f"参加者{index + 1}"
        return f"- {label} ({r_str})"

    normal = [
        format_name(uid, i, name, r_str, viewer_id)
        for i, (uid, name, r_str) in enumerate(temp_normals[:5])
    ]
    full = [
        format_name(uid, i + len(normal), name, r_str, viewer_id)
        for i, (uid, name, r_str) in enumerate(temp_normals[5:] + temp_full)
    ]

    channel = bot.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(message_id)
    embed = message.embeds[0]

    is_first_party = session['label'] == 'パーティA'
    ended = len(participants) >= 5 and is_first_party
    embed.title = f"🎮 VALORANT {session['label']}{' 🔒 募集終了' if ended else ''}"
    embed.description = (
        f"🕒 基準ランク：{base_rank_str}　フルパ：無制限\n\n"
        f"**🟢 通常参加者（条件内・最大5人）**\n" + ("\n".join(normal) if normal else "（なし）") +
        "\n\n**🔴 フルパ待機者（条件外または6人目以降）**\n" + ("\n".join(full) if full else "（なし）")
    )
    await message.edit(embed=embed, view=JoinButtonView(message_id))

    if ended and not session.get("next_posted"):
        dlog("first party reached 5; next party posting trigger")  # ← 追加
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
