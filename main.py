import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive  # Replit用

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1394558478550433802

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- ランクとティア設定 ---
RANK_GROUPS = [
    ("アイアン", ["アイアン1", "アイアン2", "アイアン3"]),
    ("ブロンズ", ["ブロンズ1", "ブロンズ2", "ブロンズ3"]),
    ("シルバー", ["シルバー1", "シルバー2", "シルバー3"]),
    ("ゴールド", ["ゴールド1", "ゴールド2", "ゴールド3"]),
    ("プラチナ", ["プラチナ1", "プラチナ2", "プラチナ3"]),
    ("ダイヤモンド", ["ダイヤモンド1", "ダイヤモンド2", "ダイヤモンド3"]),
    ("アセンダント", ["アセンダント1", "アセンダント2", "アセンダント3"]),
    ("イモータル", ["イモータル1", "イモータル2", "イモータル3"]),
    ("レディアント", ["レディアント"]),
]

TIER_LIST = sum([tiers for _, tiers in RANK_GROUPS], [])
RANK_INDEX = {tier: idx for idx, tier in enumerate(TIER_LIST)}
RANK_FACTOR = {tier: i for i, (_, tiers) in enumerate(RANK_GROUPS) for tier in tiers}
TIER_FACTOR = {tier: i for i, tier in enumerate(TIER_LIST)}

# --- 状態管理 ---
latest_message = None
participant_data = OrderedDict()  # user_id: (name, rank_str, rank_factor, tier_factor)
event_start_time = None
reminded_users = set()

# --- マッチング判定ロジック ---
def is_valid_match(new_rank, new_tier, existing_participants):
    if not existing_participants:
        return True

    factors = [(rank, tier) for (_, _, rank, tier) in existing_participants.values()]
    rank_factors = [r for r, _ in factors]
    tier_factors = [t for _, t in factors]

    if max(rank_factors) <= 4:  # プラチナ以下
        max_rank = max(rank_factors)
        min_rank = min(rank_factors)
        if abs(new_rank - max_rank) > 1 or abs(new_rank - min_rank) > 1:
            return False
        return True
    else:  # ダイヤ以上が含まれる
        max_tier = max(tier_factors)
        min_tier = min(tier_factors)
        return (new_tier >= max_tier - 3) and (new_tier <= min_tier + 3)

# --- UI: ランク選択 ---
class RankSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=rank, value=rank) for rank in TIER_LIST]
        super().__init__(placeholder="あなたのランクを選んでください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        rank_str = self.values[0]
        rank_factor = RANK_FACTOR[rank_str]
        tier_factor = TIER_FACTOR[rank_str]

        valid = is_valid_match(rank_factor, tier_factor, participant_data)

        participant_data[user_id] = (interaction.user.display_name, rank_str, rank_factor, tier_factor)
        await update_participant_embed()

        if valid:
            await interaction.followup.send(f"✅ あなたのランク「**{rank_str}**」を登録しました！", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ ランク差によりフルパ待機扱いになります。「{rank_str}」登録済み。", ephemeral=True)

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

    normal = []
    fullparty = []
    for uid, (name, r_str, r_factor, t_factor) in participant_data.items():
        if is_valid_match(r_factor, t_factor, {k: v for k, v in participant_data.items() if k != uid}):
            normal.append(f"- {name}（{r_str}）")
        else:
            fullparty.append(f"- {name}（{r_str}）")

    embed = latest_message.embeds[0]
    embed.title = "🎮 コンペ定期募集：ランク参加"
    embed.description = (
        "🕒 開始時間　21:00\n\n"
        "**🟢 通常参加者（有効ランク差内）**\n"
        + ("\n".join(normal) if normal else "（なし）") +
        "\n\n**🔴 フルパ待機者（ランク差あり）**\n"
        + ("\n".join(fullparty) if fullparty else "（なし）")
    )

    view = None if len(participant_data) >= 5 else JoinButtonView()
    await latest_message.edit(embed=embed, view=view)

# --- 18:30 投稿ループ ---
@tasks.loop(minutes=1)
async def daily_poster():
    global latest_message, participant_data, event_start_time, reminded_users
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(jst)

    if now.hour == 16 and now.minute == 51:
        participant_data.clear()
        reminded_users.clear()
        today = now.date()
        event_start_time = jst.localize(datetime.datetime.combine(today, datetime.time(21, 0)))

        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🎮 コンペ定期募集：ランク参加",
                description="🕒 開始時間　21:00\n\n**🟢 通常参加者（有効ランク差内）**\n（なし）\n\n**🔴 フルパ待機者（ランク差あり）**\n（なし）",
                color=discord.Color.blurple(),
                timestamp=now
            )
            embed.set_footer(text="参加希望の方は下のボタンをクリックしてください")
            latest_message = await channel.send(content="テストです", embed=embed, view=JoinButtonView())

# --- 5分前通知 ---
@tasks.loop(minutes=1)
async def reminder_task():
    if event_start_time is None:
        return

    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(jst)
    delta = (event_start_time - now).total_seconds()

    if 0 < delta <= 300:
        channel = bot.get_channel(CHANNEL_ID)
        mentions = []
        for uid, (_, _, r, t) in participant_data.items():
            if uid not in reminded_users and is_valid_match(r, t, participant_data):
                mentions.append(f"<@{uid}>")
                reminded_users.add(uid)

        if mentions and channel:
            await channel.send(f"🔔 {', '.join(mentions)} ゲーム開始まであと5分です！準備はOK？")

# --- 起動処理 ---
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
