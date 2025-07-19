import discord
from discord.ext import commands, tasks
import datetime
import pytz
import os
from collections import OrderedDict
from keep_alive import keep_alive  # Replit用

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1394558478550433802  # 投稿先チャンネルのID

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- ランク一覧とインデックス ---
RANK_TIER_LIST = [
    "アイアン1", "アイアン2", "アイアン3",
    "ブロンズ1", "ブロンズ2", "ブロンズ3",
    "シルバー1", "シルバー2", "シルバー3",
    "ゴールド1", "ゴールド2", "ゴールド3",
    "プラチナ1", "プラチナ2", "プラチナ3",
    "ダイヤモンド1", "ダイヤモンド2", "ダイヤモンド3",
    "アセンダント1", "アセンダント2", "アセンダント3",
    "イモータル1", "イモータル2", "イモータル3",
    "レディアント"
]
RANK_INDEX = {rank: i for i, rank in enumerate(RANK_TIER_LIST)}

# --- 状態管理 ---
latest_message = None
participant_data = OrderedDict()  # user_id: (name, rank_str, rank_index)
base_rank_index = None
event_start_time = None
reminded_users = set()

# --- UI: ランク選択 ---
class RankSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=rank, value=rank) for rank in RANK_TIER_LIST]
        super().__init__(placeholder="あなたのランクを選んでください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        global base_rank_index
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        rank_str = self.values[0]
        rank_index = RANK_INDEX[rank_str]
        participant_data[user_id] = (interaction.user.display_name, rank_str, rank_index)

        update_base_rank_index()
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
        user_id = interaction.user.id
        if user_id in participant_data:
            del participant_data[user_id]
            await interaction.response.send_message("❌ 参加を取り消しました。", ephemeral=True)
            update_base_rank_index()
            await update_participant_embed()
        else:
            await interaction.response.send_message("⚠️ まだ参加していません。", ephemeral=True)

# --- ランク基準の更新 ---
def update_base_rank_index():
    global base_rank_index
    if participant_data:
        for uid, (_, _, rank_index) in participant_data.items():
            base_rank_index = rank_index
            break
    else:
        base_rank_index = None

# --- 埋め込み更新 ---
async def update_participant_embed():
    if not latest_message:
        return

    normal_participants = []
    full_party_waiting = []

    for uid, (name, r_str, r_idx) in participant_data.items():
        if base_rank_index is not None and abs(r_idx - base_rank_index) <= 3:
            normal_participants.append(f"- {name}（{r_str}）")
        else:
            full_party_waiting.append(f"- {name}（{r_str}）")

    embed = latest_message.embeds[0]
    embed.description = (
        "🕒 定期募集：コンペ(21:00開始)\n\n"
        "**🟢 通常参加者（±3ティア以内）**\n"
        + ("\n".join(normal_participants) if normal_participants else "（なし）")
        + "\n\n**🔴 フルパ待機者（差が大きいため）**\n"
        + ("\n".join(full_party_waiting) if full_party_waiting else "（なし）")
    )

    if len(participant_data) >= 5:
        embed.title = "🎮 VALORANT 定期募集（21:00 開始予定）【募集終了】"
        view = None
    else:
        view = JoinButtonView()

    await latest_message.edit(embed=embed, view=view)

# --- 18:30 投稿ループ ---
@tasks.loop(minutes=1)
async def daily_poster():
    global latest_message, participant_data, base_rank_index, event_start_time, reminded_users

    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(jst)

    if now.hour == 18 and now.minute == 30:
        participant_data = OrderedDict()
        base_rank_index = None
        reminded_users = set()

        today = now.date()
        event_start_time = jst.localize(datetime.datetime.combine(today, datetime.time(21, 00)))

        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🎮 VALORANT 定期募集（21:00 開始予定）",
                description="🕒 定期募集：一緒にプレイしませんか？\n\n**🟢 通常参加者（±3ティア以内）**\n（なし）\n\n**🔴 フルパ待機者（差が大きいため）**\n（なし）",
                color=discord.Color.blurple(),
                timestamp=now
            )
            embed.set_footer(text="参加希望の方は下のボタンをクリックしてください")
            latest_message = await channel.send(embed=embed, view=JoinButtonView())

# --- 5分前通知ループ ---
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
        for uid, (_, _, r_idx) in participant_data.items():
            if abs(r_idx - base_rank_index) <= 3 and uid not in reminded_users:
                mentions.append(f"<@{uid}>")
                reminded_users.add(uid)

        if mentions and channel:
            await channel.send(f"🔔 {', '.join(mentions)} ゲーム開始まであと5分です！準備はOK？")

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