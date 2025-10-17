# bot.py —— HRS(全ログ) / QBOX(モーダルのみ) / HEALTH(モーダル→health_logs) + ヘルスHTTP

import os, sys, traceback
import discord
import requests
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv
from aiohttp import web

print("[boot] starting bot.py")

# ===== ヘルスチェックHTTP（Railway延命/監視用） =====
async def _health(request):
    return web.Response(text="ok")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", _health)])
    runner = web.AppRunner(app); await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] health server on :{port}")

# ===== .env / Variables =====
load_dotenv()
TOKEN    = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
GAS_URL  = os.getenv("GAS_URL")
GAS_KEY  = os.getenv("GAS_SHARED_TOKEN", "")

def _parse_ids(s: str | None) -> set[int]:
    if not s: return set()
    out = set()
    for part in s.split(","):
        p = part.strip()
        if not p: continue
        try:
            out.add(int(p))
        except ValueError:
            pass
    return out

HRS_IDS     = _parse_ids(os.getenv("CHANNEL_HRS"))      # ほうれんそう（通常メッセージも収集）
QBOX_IDS    = _parse_ids(os.getenv("CHANNEL_QBOX"))     # 質問箱（モーダルのみ）
HEALTH_IDS  = _parse_ids(os.getenv("CHANNEL_HEALTH"))   # 健康・お薬相談（モーダルのみ→別シート）

if not TOKEN or TOKEN.strip() == "":
    print("[boot][ERROR] DISCORD_TOKEN 未設定"); sys.exit(1)

print("[boot] env ok. token length:", len(TOKEN))
print("[boot] HRS_IDS=", HRS_IDS, "QBOX_IDS=", QBOX_IDS, "HEALTH_IDS=", HEALTH_IDS)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== チャンネル別タグ定義 =====
TAG_SETS: dict[int, list[tuple[str, str]]] = {}
for cid in HRS_IDS:
    TAG_SETS[cid] = [("報告", "#報告 "), ("連絡", "#連絡 "), ("相談", "#相談 "), ("共有", "#共有 ")]
for cid in QBOX_IDS:
    TAG_SETS[cid] = [("質問", "#質問 "), ("相談", "#相談 "), ("提案", "#提案 ")]
for cid in HEALTH_IDS:
    TAG_SETS[cid] = [("健康相談", "#健康相談 "), ("お薬相談", "#お薬相談 ")]

STYLE_ROTATION = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.success,
    discord.ButtonStyle.danger,
    discord.ButtonStyle.secondary,
]
# ===== モーダル：押す→本文入力→GAS直送 =====
class TagInputModal(ui.Modal, title="記録内容を入力"):
    def __init__(self, tag_text: str, sheet_key: str):
        super().__init__(timeout=300)
        self.tag_text = tag_text
        self.sheet_key = sheet_key   # "default" or "health"
        self.text = ui.TextInput(
            label="本文（任意）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            placeholder="例）#健康相談 ○○の症状が…"
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        if not GAS_URL:
            await interaction.followup.send("GAS_URL 未設定（Variables）", ephemeral=True)
            return

        chan_name = getattr(interaction.channel, "name", str(interaction.channel_id))
        content = (self.tag_text + (self.text.value or "")).strip()

        payload = {
            "token": GAS_KEY,
            "channel": chan_name,
            "user": interaction.user.display_name,
            "content": content,
            # ← これでGAS側がシートを出し分けできる
            "sheet": self.sheet_key,      # "default" or "health"
        }

        try:
            r = requests.post(GAS_URL, json=payload, timeout=12)
            print(f"[modal->POST] {r.status_code}")
            await interaction.followup.send(f"記録しました（{r.status_code}）", ephemeral=True)
        except Exception as e:
            print("[modal POST error]:", e)
            await interaction.followup.send(f"送信エラー: {e}", ephemeral=True)

# ===== 常設ボタン View（チャンネル単位） =====
class PersistentTagView(ui.View):
    """ custom_id = tag:{channel_id}:{index} """
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        items = TAG_SETS.get(channel_id) or []
        for i, (label, tag_text) in enumerate(items):
            style = STYLE_ROTATION[i % len(STYLE_ROTATION)]
            custom_id = f"tag:{channel_id}:{i}"
            self.add_item(ui.Button(label=label, style=style, custom_id=custom_id))

# ===== ボタン押下 → モーダル（シート振り分け） =====
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    data = interaction.data or {}
    cid = data.get("custom_id", "")
    if not isinstance(cid, str) or not cid.startswith("tag:"):
        return
    try:
        _, chan_id_str, idx_str = cid.split(":")
        chan_id = int(chan_id_str); idx = int(idx_str)
    except Exception:
        return

    items = TAG_SETS.get(chan_id)
    if not items or not (0 <= idx < len(items)):
        return

    _, tag_text = items[idx]

    # HEALTHチャンネルなら health シートへ、他は default
    sheet_key = "health" if chan_id in HEALTH_IDS else "default"
    await interaction.response.send_modal(TagInputModal(tag_text, sheet_key))

# ===== /tags_pin（各チャンネル本体で実行→ピン留め推奨） =====
@bot.tree.command(name="tags_pin", description="このチャンネルにタグボタンを常設します（公開）")
async def tags_pin(interaction: discord.Interaction):
    cid = interaction.channel_id
    items = TAG_SETS.get(cid)
    if not items:
        await interaction.response.send_message(
            "このチャンネル用のタグセットが未定義です（Variablesの CHANNEL_* にこのチャンネルIDを追加してください）。",
            ephemeral=True
        )
        return
    await interaction.response.send_message("タグを選んでください：",
                                            view=PersistentTagView(cid),
                                            ephemeral=False)

# ===== /log（手動送信） =====
@bot.tree.command(name="log", description="内容をスプレッドシートに記録します")
async def _log(interaction: discord.Interaction, content: str):
    await interaction.response.defer(ephemeral=True)
    if not GAS_URL:
        await interaction.followup.send("GAS_URL未設定（Variables）", ephemeral=True)
        return

    chan_obj = interaction.channel
    chan_name = getattr(chan_obj, "name", None) or (
        f"{interaction.guild.name}#{interaction.channel_id}" if interaction.guild else str(interaction.channel_id)
    )

    # 手動ログは「基本 default」。健康相談チャンネルで使われたら health に寄せてもOK
    sheet_key = "health" if interaction.channel_id in HEALTH_IDS else "default"

    payload = {
        "token": GAS_KEY,
        "channel": chan_name,
        "user": interaction.user.display_name,
        "content": content,
        "sheet": sheet_key,
    }
    try:
        r = requests.post(GAS_URL, json=payload, timeout=12)
        print("[/log->POST]", r.status_code, r.text[:120])
        await interaction.followup.send(f"status={r.status_code}, body={r.text[:120]}", ephemeral=True)
    except Exception as e:
        print("[/log POST error]", e)
        await interaction.followup.send(f"送信エラー: {e}", ephemeral=True)

@bot.tree.command(name="ping", description="応答テスト")
async def _ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)
# ===== HRS用：通常メッセージの自動収集 =====
def _in_targets(message: discord.Message, targets: set[int]) -> bool:
    cid = getattr(message.channel, "id", None)
    pid = getattr(getattr(message.channel, "parent", None), "id", None)
    return bool(targets) and (cid in targets or (pid and pid in targets))

@bot.event
async def on_message(message: discord.Message):
    try:
        gid = getattr(message.guild, "id", None)
        cid = getattr(message.channel, "id", None)
        cname = getattr(message.channel, "name", None)
        pid = getattr(getattr(message.channel, "parent", None), "id", None)
        print(f"[recv] guild={gid} ch={cid} name={cname} parent={pid} "
              f"author_bot={message.author.bot} len={len(message.content or '')}")
    except Exception as e:
        print("[recv] log error:", e)

    if message.author.bot:
        return

    # HRSのみ自動収集
    if _in_targets(message, HRS_IDS) and GAS_URL:
        payload = {
            "token": GAS_KEY,
            "channel": cname or str(cid),
            "user": message.author.display_name,
            "content": message.content or "",
            "sheet": "default",   # 自動収集は既存シートへ
        }
        try:
            r = requests.post(GAS_URL, json=payload, timeout=8)
            print(f"[on_message->POST] -> {r.status_code}")
        except Exception as e:
            print("[on_message POST error]:", e)

    await bot.process_commands(message)

# ===== 同期 & 起動 =====
@bot.event
async def setup_hook():
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"[sync] to guild {GUILD_ID}: {[c.name for c in synced]}")
        else:
            synced = await bot.tree.sync()
            print(f"[sync] global: {[c.name for c in synced]}")
    except Exception:
        print("[sync][ERROR]")
        traceback.print_exc()

@bot.event
async def on_ready():
    print(f"[ready] Logged in as {bot.user} (id={bot.user.id})")
    print("[ready] Guilds:", [(g.name, g.id) for g in bot.guilds])
    print("[ready] HRS_IDS =", HRS_IDS, "QBOX_IDS =", QBOX_IDS, "HEALTH_IDS =", HEALTH_IDS)

    # 永続ボタン（定義済みチャンネル分ぜんぶ）
    for cid in TAG_SETS.keys():
        bot.add_view(PersistentTagView(cid))
    print("[ready] persistent views registered")

    # ヘルスHTTP
    if not getattr(bot, "_web_started", False):
        bot._web_started = True
        bot.loop.create_task(start_web())

# ===== 起動 =====
try:
    print("[run] starting client...")
    bot.run(TOKEN)
except Exception:
    print("[run][FATAL] uncaught exception")
    traceback.print_exc()
    sys.exit(1)
