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

party_sessions = OrderedDict()  # message_id: {label, participants, start_time, reminded, next_posted}
party_labels = ['パーティA', 'パーティB']
max_party_count = 2
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

async def update_embed(message_id):
    session = party_sessions[message_id]
    participants = session["participants"]
    base_rank_str, base_rank, base_tier = get_base_participant(participants)

    temp_normals = []
    temp_full = []
    count = 0
    for uid, (name, r_str, r, t) in participants.items():
        count += 1
        if count == 1:
            temp_normals.append((uid, name))  # 基準ランク
        elif 2 <= count <= 3 and base_rank is not None and is_valid_by_base(r, t, base_rank, base_tier):
            temp_normals.append((uid, name))  # 条件内
        elif count == 4:
            temp_full.append((uid, name))  # 一時待機者（5人目が来るまで）
        elif count == 5:
            temp_normals.append((uid, name))  # 無条件参加
            if len(temp_full) == 1:
                # 4人目を待機から昇格
                temp_normals.insert(3, temp_full.pop(0))
        else:
            temp_full.append((uid, name))  # 6人目以降

    normal = [f"- {name}" for _, name in temp_normals[:5]]
    full = [f"- {name}" for _, name in temp_normals[5:]] + [f"- {name}" for _, name in temp_full]

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
        session["next_posted"] = True
        if len(party_sessions) < max_party_count:
            await post_party_embed()
