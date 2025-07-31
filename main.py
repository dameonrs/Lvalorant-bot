import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive

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

# --- 状態管理 ---
latest_message = None
participant_data = OrderedDict()
event_start_time = None
reminded_users = set()

def is_valid_by_base(new_rank, new_tier, base_rank, base_tier):
    if new_tier >= 25 or base_tier >= 25:
        if abs(new_tier - base_tier) > 6:
            return False
        return base_tier - 3 <= new_tier <= base_tier + 3
    else:
        return abs(new_rank - base_rank) <= 1

def get_base_participant():
    for _, (_, rank_str, rank, tier) in participant_data.items():
        return rank_str, rank, tier
    return "未設定", None, None

# --- 埋め込み更新（最終調整済） ---
async def update_participant_embed():
    if not latest_message:
        return

    base_rank_str, base_rank, base_tier = get_base_participant()

    normal_participants = []
    fullparty_participants = []

    if base_rank is not None:
        for i, (uid, (name, rank_str, rank, tier)) in enumerate(participant_data.items()):
            if i == 0:
                normal_participants.append((uid, name))
            elif i == 3:
                fullparty_participants.append((uid, name))
            elif i == 4:
                normal_participants.append((uid, name))
            elif is_valid_by_base(rank, tier, base_rank, base_tier):
                normal_participants.append((uid, name))
            else:
                fullparty_participants.append((uid, name))

        normal = [f"- {name}" for _, name in normal_participants]
        full = [f"- {name}" for _, name in fullparty_participants]
    else:
        base_rank_str = "未設定"
        normal = []
        full = []

    embed = latest_message.embeds[0]
    embed.title = "🎮 VALORANT 定期募集（21:00 開始予定）"
    embed.description = (
        f"🕒 基準ランク：{base_rank_str}　フルパ：無制限\n\n"
        "**🟢 通常参加者（条件内・最大5人）**\n" + ("\n".join(normal) if normal else "（なし）") +
        "\n\n**🔴 フルパ待機者（条件外または4人目・6人目以降）**\n" + ("\n".join(full) if full else "（なし）")
    )

    view = JoinButtonView()
    await latest_message.edit(embed=embed, view=view)

# --- ボタンビュー（継続） ---
class JoinButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎮 参加する", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
        if event_start_time and now >= event_start_time:
            await interaction.response.send_message("⚠️ 開始時間を過ぎているため、参加できません。", ephemeral=True)
            return

        if interaction.user.id in participant_data:
            await interaction.response.send_message("✅ 既に参加済みです。ランクを再登録するには選び直してください。", ephemeral=True)
        else:
            await interaction.response.send_message("🔽 ランクを選んでください：", view=RankSelectView(), ephemeral=True)

    @discord.ui.button(label="❌ 取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
        if event_start_time and now >= event_start_time:
            await interaction.response.send_message("⚠️ 開始時間を過ぎているため、取り消しできません。", ephemeral=True)
            return

        if interaction.user.id in participant_data:
            del participant_data[interaction.user.id]
            await update_participant_embed()
            await interaction.response.send_message("❌ 取り消しました。", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ まだ参加していません。", ephemeral=True)

# --- ランク選択 ---
class RankSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=rank) for rank in TIER_MAP.keys()]
        super().__init__(placeholder="ランクを選んでください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        rank_str = self.values[0]
        tier = TIER_MAP[rank_str]
        base = rank_str.rstrip("123")
        rank = RANK_FACTORS.get(base)

        if rank is None:
            await interaction.followup.send("⚠️ ランク解析に失敗しました。", ephemeral=True)
            return

        participant_data[interaction.user.id] = (interaction.user.display_name, rank_str, rank, tier)
        await update_participant_embed()
        await interaction.followup.send(f"✅ ランク「**{rank_str}**」を登録しました！", ephemeral=True)

class RankSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RankSelect())

# --- 定期投稿と通知処理・起動 ---
@tasks.loop(minutes=1)
async def daily_poster():
    global latest_message, participant_data, event_start_time, reminded_users

    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    if now.hour == 19 and now.minute == 10:
        participant_data.clear()
        reminded_users.clear()
        event_start_time = now.replace(hour=21, minute=0, second=0, microsecond=0)

        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🎮 VALORANT 定期募集（21:00 開始予定）",
                description="🕒 基準ランク：未設定　フルパ：無制限\n\n"
                            "**🟢 通常参加者（条件内・最大5人）**\n（なし）\n\n"
                            "**🔴 フルパ待機者（条件外または4人目・6人目以降）**\n（なし）",
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="参加希望の方は下のボタンをクリックしてください")
            latest_message = await channel.send(content="@everyone", embed=embed, view=JoinButtonView())

@tasks.loop(minutes=1)
async def reminder_task():
    if event_start_time is None:
        return

    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    delta = (event_start_time - now).total_seconds()
    if 0 < delta <= 300:
        base_rank_str, base_rank, base_tier = get_base_participant()
        if base_rank is None:
            return

        channel = bot.get_channel(CHANNEL_ID)
        mentions = []

        for uid, (_, _, r, t) in participant_data.items():
            if is_valid_by_base(r, t, base_rank, base_tier) and uid not in reminded_users:
                mentions.append(f"<@{uid}>")
                reminded_users.add(uid)

        if mentions and channel:
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
