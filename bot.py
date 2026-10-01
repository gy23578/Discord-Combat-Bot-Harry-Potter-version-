import os
import random
import discord
import asyncio

from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


players = {}
duel_requests = {}
active_duels = {}
pending_attacks = {}


def get_player(user):
    if user.id not in players:
        players[user.id] = {
            "name": user.display_name,
            "hp": 100,
            "level": 1,
            "xp": 0,
            "spellslevel": {
                "confringo": 1,
                "protego": 1}}


    return players[user.id]


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user}")


@bot.command()
async def test(ctx):
    await ctx.send("Le bot fonctionne 🪄")


@bot.command()
async def profil(ctx):
    player = get_player(ctx.author)

    await ctx.send(
        f"🧙 {player['name']}\n"
        f"❤️ PV : {player['hp']}/100\n"
        f"⭐ Niveau : {player['level']}\n"
        f"✨ XP : {player['xp']}"
    )

#----------------------------------- CONFRINGO---------------------------------------------------- #
@bot.command()
async def confringo(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("Tu n'es dans aucun duel.")
        return

    opponent_id = active_duels[ctx.author.id]
    opponent = await bot.fetch_user(opponent_id)


    attacker = get_player(ctx.author)
    defender = get_player(opponent)

    damage = random.randint(15, 30)
    attack_power = (
            random.randint(1, 20)
            + attacker["level"] * 2
            + attacker["spellslevel"]["confringo"] * 5
    )
    pending_attacks[opponent.id] = {
        "attacker_id": ctx.author.id,
        "spell": "confringo",
        "damage": damage,
    "power": attack_power

    }



    await ctx.send(
        f"🔥 {ctx.author.display_name} lance Confringo sur {opponent.display_name} !\n"
        f"🛡️ {opponent.display_name} a 10 secondes pour utiliser `!protego` !"
    )

    await asyncio.sleep(10)

    if opponent.id in pending_attacks:
        defender["hp"] -= damage

        if defender["hp"] < 0:
            defender["hp"] = 0

        del pending_attacks[opponent.id]

        await ctx.send(
            f"💥 {opponent.display_name} n'a pas réussi à se défendre !\n"
            f"🔥 Confringo inflige **{damage} dégâts**.\n"
            f"❤️ {opponent.display_name} : **{defender['hp']}/100 PV**"
        )
#----------------------------------- DUEL---------------------------------------------------- #

@bot.command()
async def duel(ctx, target: discord.Member):
    if target.id == ctx.author.id:
        await ctx.send("Tu ne peux pas te défier toi-même.")
        return

    if ctx.author.id in active_duels or target.id in active_duels:
        await ctx.send("L'un de vous est déjà en duel.")
        return

    duel_requests[target.id] = ctx.author.id

    await ctx.send(
        f"⚔️ {ctx.author.display_name} défie {target.display_name} en duel !\n"
        f"{target.display_name}, utilise `!accepter` ou `!refuser`."
    )

#----------------------------------- REFUSE---------------------------------------------------- #
@bot.command()
async def refuser(ctx):
    if ctx.author.id not in duel_requests:
        await ctx.send("Tu n'as aucune demande de duel en attente.")
        return

    challenger_id = duel_requests[ctx.author.id]

    challenger = await bot.fetch_user(challenger_id)

    del duel_requests[ctx.author.id]

    await ctx.send(
        f"❌ {ctx.author.display_name} a refusé le duel de "
        f"{challenger.display_name}."
    )

#----------------------------------- ACCEPT---------------------------------------------------- #
@bot.command()
async def accepter(ctx):
    if ctx.author.id not in duel_requests:
        await ctx.send("Tu n'as aucune demande de duel en attente.")
        return

    challenger_id = duel_requests[ctx.author.id]

    active_duels[ctx.author.id] = challenger_id
    active_duels[challenger_id] = ctx.author.id

    del duel_requests[ctx.author.id]

    challenger = await bot.fetch_user(challenger_id)

    await ctx.send(
        f"⚔️ Duel accepté !\n"
        f"{challenger.display_name} VS {ctx.author.display_name}\n"
        f"Que le duel commence 🪄"
    )

    # ----------------------------------- PROTEGO---------------------------------------------------- #
@bot.command()
async def protego(ctx):
    if ctx.author.id not in pending_attacks:
        await ctx.send("🛡️ Il n'y a aucune attaque à bloquer.")
        return

    attack = pending_attacks[ctx.author.id]

    defender = get_player(ctx.author)

    defense_power = (
        random.randint(1, 20)
        + defender["level"] * 2
        + defender["spells"]["protego"] * 5
    )

    if defense_power >= attack["power"]:

        del pending_attacks[ctx.author.id]

        await ctx.send(
            f"🛡️ **{ctx.author.display_name} lance PROTEGO !**\n"
            f"✨ Protego bloque complètement l'attaque !\n"
            f"⚔️ Attaque : **{attack['power']}**\n"
            f"🛡️ Défense : **{defense_power}**"
        )

    else:
        damage = attack["damage"]

        defender["hp"] -= damage

        if defender["hp"] < 0:
            defender["hp"] = 0

        del pending_attacks[ctx.author.id]

        await ctx.send(
            f"🛡️ **{ctx.author.display_name} lance PROTEGO !**\n"
            f"💥 Protego est brisé !\n"
            f"⚔️ Attaque : **{attack['power']}**\n"
            f"🛡️ Défense : **{defense_power}**\n"
            f"🔥 Dégâts reçus : **{damage}**\n"
            f"❤️ PV : **{defender['hp']}/100**"
        )























bot.run(TOKEN)


