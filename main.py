import asyncio
import io
import os
import time
import urllib.parse
import aiohttp
from google import genai
from google.genai import types
from groq import Groq
import discord
from discord import app_commands
import psutil
import httpx
import libsql_client

# Konfiguracja – klucze pobierane ze zmiennych środowiskowych (bezpieczne na GitHubie!)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

global_cooldown_until = 0
OWNER_ID = 1497880466546753687
AI_CHANNEL_ID = 1548561533079003216

ai_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

DEFAULT_SYSTEM_INSTRUCTION = "You are a helpful Discord assistant. Respond in English unless requested otherwise or if the context demands a different language."

# Inicjalizacja bazy libSQL
db_client = libsql_client.create_client_sync("file:bot_database.db")

db_client.execute("""
    CREATE TABLE IF NOT EXISTS channel_settings (
        channel_id INTEGER PRIMARY KEY,
        mode_name TEXT,
        system_instruction TEXT,
        text_model TEXT DEFAULT 'openai/gpt-oss-20b'
    )
""")


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
    print("Hybrid Slash Commands synchronized.")


client = BotClient()
channel_histories = {}


def get_channel_instruction(channel_id: int):
  result = db_client.execute(
      "SELECT system_instruction FROM channel_settings WHERE channel_id = ?",
      (channel_id,),
  )
  row = result.rows
  return row[0][0] if row and row[0][0] else DEFAULT_SYSTEM_INSTRUCTION


def get_channel_model(channel_id: int):
  result = db_client.execute(
      "SELECT text_model FROM channel_settings WHERE channel_id = ?",
      (channel_id,),
  )
  row = result.rows
  return row[0][0] if row and row[0][0] else "openai/gpt-oss-20b"


def get_channel_history(channel_id: int):
  if channel_id not in channel_histories:
    channel_histories[channel_id] = []
  return channel_histories[channel_id]


@client.event
async def on_ready():
  print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
  if message.author == client.user:
    return

  if client.user in message.mentions:
    try:
      await message.add_reaction("🖕")
    except Exception as e:
      print(f"Failed to add reaction: {e}")

    await message.channel.send("Don't ping me, idiot.")
    return


@client.tree.command(name="scan", description="Scan the state of libSQL database, Gemini API, and Groq API")
async def scan_command(interaction: discord.Interaction):
  if interaction.user.id != OWNER_ID:
    await interaction.response.send_message(
        "❌ You do not have permission to use this command!", ephemeral=True
    )
    return

  await interaction.response.defer(thinking=True)

  try:
    test_client = libsql_client.create_client_sync("file:bot_database.db")
    test_result = test_client.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = test_result.rows
    test_client.close()
    sqlite_status = f"✅ libSQL is working properly (tables: {len(tables)})"
  except Exception as e:
    sqlite_status = f"❌ libSQL Error: {str(e)}"

  try:
    gem_chat = gemini_client.chats.create(model="gemini-3.5-flash-lite")
    gem_res = gem_chat.send_message("Test")
    if gem_res and gem_res.text:
      gemini_status = "✅ Gemini API is responding properly"
    else:
      gemini_status = "⚠️ Gemini API responded, but content is empty"
  except Exception as e:
    gemini_status = f"❌ Gemini API Error: {str(e)[:100]}"

  try:
    groq_test = ai_client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": "Test"}],
        max_tokens=10
    )
    if groq_test and groq_test.choices:
      groq_status = "✅ Groq API is responding properly"
    else:
      groq_status = "⚠️ Groq API responded, but no response body"
  except Exception as e:
    groq_status = f"❌ Groq API Error: {str(e)[:100]}"

  embed = discord.Embed(
      title="🔍 System Scan Results",
      color=discord.Color.blue()
  )
  embed.add_field(name="🗄 Database", value=sqlite_status, inline=False)
  embed.add_field(name="✨ Artificial Intelligence", value=f"{gemini_status}\n{groq_status}", inline=False)
  embed.set_footer(
      text=f"Scan requested by: {interaction.user.display_name}",
      icon_url=client.user.display_avatar.url,
  )

  await interaction.followup.send(embed=embed)


@client.tree.command(name="ai", description="Chat, analyze images, or generate artwork")
@app_commands.describe(
    pytanie="Your question to the bot", 
    zdjecie="Image to analyze",
    generowanie="Description of the image you want to generate"
)
async def ai_command(
    interaction: discord.Interaction,
    pytanie: str = None,
    zdjecie: discord.Attachment = None,
    generowanie: str = None,
):
  global global_cooldown_until

  if not interaction.guild:
    await interaction.response.send_message(
        "❌ AI commands work only inside a server!",
        ephemeral=True,
    )
    return

  if interaction.channel.id != AI_CHANNEL_ID:
    await interaction.response.send_message(
        "❌ You can only use `/ai` commands in the designated AI channel!",
        ephemeral=True,
    )
    return

  if not pytanie and not zdjecie and not generowanie:
    await interaction.response.send_message(
        "Hello! Provide a question, attach an image, or give a prompt to generate an image.",
        ephemeral=True,
    )
    return

  await interaction.response.defer()

  try:
    if generowanie:
      czyste = generowanie.lower().replace("generate", "").replace("generate me", "").strip()
      
      slownik = {
          "car": "a modern sports car driving on a highway",
          "airplane pilot": "an airplane pilot in uniform inside a cockpit",
          "dog": "a cute dog playing in a park",
          "cat": "a cute cat sitting on a windowsill",
          "house": "a cozy modern house surrounded by nature"
      }
      
      if czyste in slownik:
        english_prompt = slownik[czyste]
      else:
        english_prompt = f"detailed high quality digital art of {czyste}"

      encoded_prompt = urllib.parse.quote(english_prompt)
      image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"

      async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as resp:
          if resp.status == 200:
            image_bytes = await resp.read()
            file = discord.File(fp=io.BytesIO(image_bytes), filename="generated.png")

            embed = discord.Embed(
                title="🎨 Generated Image",
                description=f"**Original:** {generowanie}\n**Prompt EN:** `{english_prompt}`",
                color=discord.Color.purple()
            )
            embed.set_image(url="attachment://generated.png")
            embed.set_footer(
                text=f"Generated for: {interaction.user.display_name}",
                icon_url=client.user.display_avatar.url,
            )

            await interaction.followup.send(embed=embed, file=file)
            return
          else:
            await interaction.followup.send("❌ Failed to generate the image.", ephemeral=True)
            return

    if zdjecie:
      if not zdjecie.content_type or not zdjecie.content_type.startswith(
          "image/"
      ):
        await interaction.followup.send(
            "❌ The uploaded file is not an image!", ephemeral=True
        )
        return

      image_bytes = await zdjecie.read()
      prompt_text = (
          pytanie if pytanie else "Describe this image and answer in English."
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
          text=f"Image from: {interaction.user.display_name}",
          icon_url=client.user.display_avatar.url,
      )

      await interaction.followup.send(embed=embed)
      return

    current_time = time.time()
    if current_time < global_cooldown_until:
      remaining_seconds = int(global_cooldown_until - current_time)
      await interaction.followup.send(
          f"⏳ **API rate limit active!** Please wait about"
          f" **{remaining_seconds} seconds**.",
          ephemeral=True,
      )
      return

    target_member = interaction.user
    cached_member = interaction.guild.get_member(interaction.user.id)
    if cached_member:
      target_member = cached_member

    spotify_info = ""
    if target_member.activities:
      for activity in target_member.activities:
        if isinstance(activity, discord.Spotify):
          spotify_info = f"[User is currently listening to Spotify: '{activity.title}' by '{activity.artist}'] "
          break

    channel_id = interaction.channel.id
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

    history.append({"role": "user", "content": pytanie if pytanie else "What am I listening to?"})
    history.append({"role": "assistant", "content": response_text})

    if len(history) > 10:
      channel_histories[channel_id] = history[-10:]

    embed = discord.Embed(
        description=response_text[:4000], color=discord.Color.green()
    )
    embed.set_footer(
        text=f"Query by: {interaction.user.display_name}",
        icon_url=client.user.display_avatar.url,
    )

    await interaction.followup.send(embed=embed)

  except Exception as e:
    error_str = str(e)
    print(f"API Error: {e}", flush=True)

    if "429" in error_str or "rate_limit" in error_str.lower():
      cooldown_duration = 30
      global_cooldown_until = time.time() + cooldown_duration
      await interaction.followup.send(
          f"⏳ **Rate limit reached!** Please wait about **{cooldown_duration}"
          " seconds**.",
          ephemeral=True,
      )
    else:
      await interaction.followup.send(
          f"❌ An error occurred: `{error_str[:150]}`", ephemeral=True
      )


@client.tree.command(
    name="model",
    description="[Owner only] Change the text model for this channel.",
)
@app_commands.describe(
    model="Model name to set (e.g. openai/gpt-oss-20b)"
)
async def model_command(interaction: discord.Interaction, model: str):
  if interaction.user.id != OWNER_ID:
    await interaction.response.send_message(
        "❌ You do not have permission to use this command!", ephemeral=True
    )
    return

  channel_id = interaction.channel.id

  db_client.execute(
      """
        INSERT INTO channel_settings (channel_id, text_model) 
        VALUES (?, ?) 
        ON CONFLICT(channel_id) DO UPDATE SET text_model = excluded.text_model
    """,
      (channel_id, model),
  )

  if channel_id in channel_histories:
    channel_histories[channel_id] = []

  await interaction.response.send_message(
      f"✅ Successfully changed the text model for this channel to: `{model}`",
      ephemeral=True,
  )


client.run(DISCORD_TOKEN)
