import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive  # Replit/Render用

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1394558478550433802  # 投稿先チャンネルID

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
participant_data = OrderedDict()  # uid: (name, rank_str, rank_factor, tier_factor)
event_start_time = None
reminded_users = set()

# --- 判定ロジック ---
def is_valid_normal_participant(new_tier, new_rank, existing):
    if not existing:
        return True

    all_tiers = [new_tier] + [p[3] for p in existing.values()]
    all_ranks = [new_rank] + [p[2] for p in existing.values()]
    min_tier, max_tier = min(all_tiers), max(all_tiers)
    min_rank, max_rank = min(all_ranks), max(all_ranks)

    contains_diamond_or_higher = any(t >= 25 for t in all_tiers)

    if contains_diamond_or_higher:
        if max_tier - min_tier > 6:
            return False
        return max_tier - 3 <= new_tier <= min_tier + 3
    else:
        return abs(max_rank - min_rank) <= 1

# --- UI: ランク選択 ---
class RankSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=rank) for rank in TIER_MAP.keys()]
        super().__init__(placeholder="あなたのランクを選んでください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        rank_str = self.values[0]
        tier = TIER_MAP[rank_str]
        base_rank = rank_str.rstrip("123")  # 末尾数字を除去
        rank_group = RANK_FACTORS.get(base_rank)

        if rank_group is None:
            await interaction.followup.send("⚠️ ランクの判定に失敗しました。", ephemeral=True)
            return

        # 登録前に仮登録して判定
        temp_data = participant_data.copy()
        temp_data[user_id] = (interaction.user.display_name, rank_str, rank_group, tier)

        if is_valid_normal_participant(tier, rank_group, {k: v for k, v in temp_data.items() if k != user_id}):
            participant_data[user_id] = (interaction.user.display_name, rank_str, rank_group, tier)
        else:
            participant_data[user_id] = (interaction.user.display_name, rank_str, rank_group, tier)

        await update_participant_embed()
        await interaction.followup.send(f"✅ あなたのランク「**{rank_str}**」を登録しました！", ephemeral=True)

class RankSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RankSelect())

# --- ボタンビュー ---
class JoinButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎮 参加する", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in participant_data:
            await interaction.response.send_message("✅ 既に参加済みです。ランクを再登録する場合は選び直してください。", ephemeral=True)
        else:
            await interaction.response.send_message("🔽 ランクを選んでください：", view=RankSelectView(), ephemeral=True)

    @discord.ui.button(label="❌ 参加を取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in participant_data:
            del participant_data[interaction.user.id]
            await interaction.response.send_message("❌ 参加を取り消しました。", ephemeral=True)
            await update_participant_embed()
        else:
            await interaction.response.send_message("⚠️ まだ参加していません。", ephemeral=True)

# --- 埋め込み更新 ---
async def update_participant_embed():
    if not latest_message:
        return

    normal_participants = []
    fullparty_participants = []

    for uid, (name, rank_str, rank_factor, tier_factor) in participant_data.items():
        temp_data = {k: v for k, v in participant_data.items() if k != uid}
        if is_valid_normal_participant(tier_factor, rank_factor, temp_data):
            normal_participants.append(f"- {name}（{rank_str}）")
        else:
            fullparty_participants.append(f"- {name}（{rank_str}）")

    embed = latest_message.embeds[0]
    embed.title = "🎮 VALORANT 定期募集（21:00 開始予定）"
    embed.description = (
        "🕒 定期募集：コンペ（21:00開始）\n\n"
        "**🟢 通常参加者（条件内）**\n"
        + ("\n".join(normal_participants) if normal_participants else "（なし）") +
        "\n\n**🔴 フルパ待機者（条件外）**\n"
        + ("\n".join(fullparty_participants) if fullparty_participants else "（なし）")
    )

    view = None if len(participant_data) >= 5 else JoinButtonView()
    await latest_message.edit(embed=embed, view=view)

# --- 18:30 投稿 ---
@tasks.loop(minutes=1)
async def daily_poster():
    global latest_message, participant_data, event_start_time, reminded_users
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(jst)

    if now.hour == 17 and now.minute == 49:
        participant_data.clear()
        reminded_users.clear()
        event_start_time = jst.localize(datetime.datetime.combine(now.date(), datetime.time(21, 0)))

        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🎮 VALORANT 定期募集（21:00 開始予定）",
                description="🕒 定期募集：一緒にプレイしませんか？\n\n"
                            "**🟢 通常参加者（条件内）**\n（なし）\n\n"
                            "**🔴 フルパ待機者（条件外）**\n（なし）",
                color=discord.Color.blurple(),
                timestamp=now
            )
            embed.set_footer(text="参加希望の方は下のボタンをクリックしてください")
            latest_message = await channel.send(content="テスト", embed=embed, view=JoinButtonView())

# --- 5分前通知 ---
@tasks.loop(minutes=1)
async def reminder_task():
    if event_start_time is None:
        return

    now = datetime.datetime.now(pytz.timezone("Asia/Tokyo"))
    delta = (event_start_time - now).total_seconds()

    if 0 < delta <= 300:
        channel = bot.get_channel(CHANNEL_ID)
        mentions = []

        for uid, (_, _, rank_factor, tier_factor) in participant_data.items():
            temp_data = {k: v for k, v in participant_data.items() if k != uid}
            if is_valid_normal_participant(tier_factor, rank_factor, temp_data) and uid not in reminded_users:
                mentions.append(f"<@{uid}>")
                reminded_users.add(uid)

        if mentions and channel:
            await channel.send(f"🔔 {', '.join(mentions)} ゲーム開始まであと5分です！準備OK？")

# --- 起動時処理 ---
@bot.event
async def on_ready():
    print(f"✅ Bot is online: {bot.user}")
    if not daily_poster.is_running():
        daily_poster.start()
    if not reminder_task.is_running():
        reminder_task.start()

# --- 起動 ---
keep_alive()
bot.run(TOKEN)
