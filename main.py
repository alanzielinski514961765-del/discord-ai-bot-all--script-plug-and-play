import os
import asyncio
import sqlite3
import time
from google import genai
from google.genai import types
from groq import Groq
import discord
from discord import app_commands

global_cooldown_until = 0
OWNER_ID = 1497880466546753687
AI_CHANNEL_ID = 1548561533079003216

# Bezpieczne pobieranie kluczy z systemowych zmiennych środowiskowych
ai_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

DEFAULT_SYSTEM_INSTRUCTION = "Jesteś pomocnym asystentem na Discordzie. Niezależnie od języka wiadomości użytkownika, zawsze i bez wyjątku odpowiadaj wyłącznie w języku polskim."

db_conn = sqlite3.connect("bot_database.db", check_same_thread=False)
db_cursor = db_conn.cursor()

db_cursor.execute("""
    CREATE TABLE IF NOT EXISTS channel_settings (
        channel_id INTEGER PRIMARY KEY,
        mode_name TEXT,
        system_instruction TEXT,
        text_model TEXT DEFAULT 'openai/gpt-oss-20b'
    )
""")
db_conn.commit()


class BotClient(discord.Client):

  def __init__(self):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.presences = True
    super().__init__(intents=intents)
    self.tree = app_commands.CommandTree(self)

  async def setup_hook(self):
    await self.tree.sync()
    print(
        "Zsynchronizowano hybrydowe Slash Commands (Groq + Gemini) na"
        " Discordzie"
    )


client = BotClient()
channel_histories = {}


def get_channel_instruction(channel_id: int):
  db_cursor.execute(
      "SELECT system_instruction FROM channel_settings WHERE channel_id = ?",
      (channel_id,),
  )
  row = db_cursor.fetchone()
  return row[0] if row else DEFAULT_SYSTEM_INSTRUCTION


def get_channel_model(channel_id: int):
  db_cursor.execute(
      "SELECT text_model FROM channel_settings WHERE channel_id = ?",
      (channel_id,),
  )
  row = db_cursor.fetchone()
  return row[0] if row and row[0] else "openai/gpt-oss-20b"


def get_channel_history(channel_id: int):
  if channel_id not in channel_histories:
    channel_histories[channel_id] = []
  return channel_histories[channel_id]


@client.event
async def on_ready():
  print(f"Zalogowano jako {client.user}")


@client.event
async def on_message(message):
  if message.author == client.user:
    return

  if client.user in message.mentions:
    try:
      await message.add_reaction("🖕")
    except Exception as e:
      print(f"Nie udało się dodać reakcji: {e}")

    await message.channel.send("Nie pinguj mnie debilu")
    return


@client.tree.command(name="ai", description="Rozmawiaj lub prześlij zdjęcie")
@app_commands.rename(pytanie="pytanie", zdjecie="zdjęcie")
@app_commands.describe(
    pytanie="Twoje pytanie do bota", zdjecie="Zdjęcie do analizy przez Gemini"
)
async def ai_command(
    interaction: discord.Interaction,
    pytanie: str = None,
    zdjecie: discord.Attachment = None,
):
  global global_cooldown_until

  if not interaction.guild:
    await interaction.response.send_message(
        "❌ Komendy AI działają wyłącznie na serwerze!",
        ephemeral=True,
    )
    return

  if interaction.channel.id != AI_CHANNEL_ID:
    await interaction.response.send_message(
        "❌ Komendy `/ai` można używać wyłącznie na wyznaczonym kanale AI!",
        ephemeral=True,
    )
    return

  channel_id = interaction.channel.id

  if not pytanie and not zdjecie:
    await interaction.response.send_message(
        "Cześć! Wpisz coś po `/ai` albo dołącz zdjęcie do wiadomości.",
        ephemeral=True,
    )
    return

  await interaction.response.defer()

  try:
    if zdjecie:
      if not zdjecie.content_type or not zdjecie.content_type.startswith(
          "image/"
      ):
        await interaction.followup.send(
            "❌ Przesłany plik nie jest obrazem!", ephemeral=True
        )
        return

      image_bytes = await zdjecie.read()
      prompt_text = (
          pytanie if pytanie else "Opisz to zdjęcie i odpowiedz na nie po polsku."
      )

      chat = gemini_client.chats.create(model="gemini-3.5-flash-lite")
      response = chat.send_message([
          types.Part.from_bytes(
              data=image_bytes,
              mime_type=zdjecie.content_type,
          ),
          prompt_text,
      ])

      response_text = response.text

      embed = discord.Embed(
          description=response_text[:4000], color=discord.Color.blue()
      )
      embed.set_footer(
          text=f"Zdjęcie od: {interaction.user.display_name}",
          icon_url=client.user.display_avatar.url,
      )

      await interaction.followup.send(embed=embed)
      return

    current_time = time.time()
    if current_time < global_cooldown_until:
      remaining_seconds = int(global_cooldown_until - current_time)
      await interaction.followup.send(
          f"⏳ **Limit API wciąż aktywny!** Poczekaj jeszcze około"
          f" **{remaining_seconds} sekund**.",
          ephemeral=True,
      )
      return

    target_member = interaction.user
    if interaction.guild:
      cached_member = interaction.guild.get_member(interaction.user.id)
      if cached_member:
        target_member = cached_member

    spotify_info = ""
    if target_member.activities:
      for activity in target_member.activities:
        if isinstance(activity, discord.Spotify):
          spotify_info = f"[Użytkownik słucha teraz na Spotify utworu '{activity.title}' wykonawcy '{activity.artist}'] "
          break

    active_model = get_channel_model(channel_id)
    instruction = get_channel_instruction(channel_id)

    history = get_channel_history(channel_id)

    messages_to_send = (
        [{"role": "system", "content": instruction}] if instruction else []
    )
    messages_to_send.extend(history)
    
    final_user_content = f"{spotify_info}{pytanie}" if pytanie else spotify_info
    messages_to_send.append({"role": "user", "content": final_user_content})

    completion = ai_client.chat.completions.create(
        model=active_model,
        messages=messages_to_send,
        temperature=0.7,
        max_tokens=1024,
    )

    response_text = completion.choices[0].message.content

    history.append({"role": "user", "content": pytanie if pytanie else "Co słucham?"})
    history.append({"role": "assistant", "content": response_text})

    if len(history) > 10:
      channel_histories[channel_id] = history[-10:]

    embed = discord.Embed(
        description=response_text[:4000], color=discord.Color.green()
    )
    embed.set_footer(
        text=f"Zapytanie od: {interaction.user.display_name}",
        icon_url=client.user.display_avatar.url,
    )

    await interaction.followup.send(embed=embed)

  except Exception as e:
    error_str = str(e)
    print(f"Błąd API: {e}", flush=True)

    if "429" in error_str or "rate_limit" in error_str.lower():
      cooldown_duration = 30
      global_cooldown_until = time.time() + cooldown_duration
      await interaction.followup.send(
          f"⏳ **Limit osiągnięty!** Poczekaj około **{cooldown_duration}"
          " sekund**.",
          ephemeral=True,
      )
    else:
      await interaction.followup.send(
          f"❌ Wystąpił błąd: `{error_str[:150]}`", ephemeral=True
      )


@client.tree.command(
    name="model",
    description="[Tylko właściciel] Zmień model tekstowy dla tego kanału.",
)
@app_commands.describe(
    model="Nazwa modelu do ustawienia (np. openai/gpt-oss-20b)"
)
async def model_command(interaction: discord.Interaction, model: str):
  if interaction.user.id != OWNER_ID:
    await interaction.response.send_message(
        "❌ Nie masz uprawnień do używania tej komendy!", ephemeral=True
    )
    return

  channel_id = interaction.channel.id

  db_cursor.execute(
      """
        INSERT INTO channel_settings (channel_id, text_model) 
        VALUES (?, ?) 
        ON CONFLICT(channel_id) DO UPDATE SET text_model = excluded.text_model
    """,
      (channel_id, model),
  )
  db_conn.commit()

  if channel_id in channel_histories:
    channel_histories[channel_id] = []

  await interaction.response.send_message(
      f"✅ Pomyślnie zmieniono model tekstowy dla tego kanału na: `{model}`",
      ephemeral=True,
  )


@client.tree.command(
    name="scan",
    description="[Tylko właściciel] Skan diagnostyczny rdzenia AlanabrelekAI.",
)
async def scan_command(interaction: discord.Interaction):
  if interaction.user.id != OWNER_ID:
    await interaction.response.send_message(
        "❌ Nie masz uprawnień do używania tej komendy!", ephemeral=True
    )
    return

  await interaction.response.defer(ephemeral=True)

  groq_status = "OK"
  try:
    ai_client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=5,
    )
  except Exception:
    groq_status = "BŁĄD / LIMIT"

  gemini_status = "OK"
  try:
    gemini_client.models.generate_content(
        model="gemini-3.5-flash-lite", contents="ping"
    )
  except Exception:
    gemini_status = "BŁĄD / LIMIT"

  db_status = "OK"
  try:
    db_cursor.execute("SELECT 1")
  except Exception:
    db_status = "BŁĄD"

  embed = discord.Embed(
      title="🔍 AlanabrelekAI - Scan Mode (Diagnostyka)",
      color=discord.Color.purple(),
  )
  embed.add_field(name="Stan Bota", value="🟢 Działa poprawnie", inline=False)
  embed.add_field(name="Rdzeń Groq API", value=groq_status, inline=True)
  embed.add_field(name="Rdzeń Gemini API", value=gemini_status, inline=True)
  embed.add_field(name="Baza Danych SQLite", value=db_status, inline=True)
  embed.add_field(
      name="Aktywne sesje historii",
      value=str(len(channel_histories)),
      inline=False,
  )
  embed.set_footer(text=f"Skan wykonany dla właściciela: {interaction.user.name}")

  await interaction.followup.send(embed=embed, ephemeral=True)


# Pobieranie tokenu bota ze zmiennych środowiskowych
client.run(os.getenv("DISCORD_BOT_TOKEN"))
