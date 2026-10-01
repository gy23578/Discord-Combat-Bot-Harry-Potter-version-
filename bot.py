import os
import random
import discord
import asyncio
import time

from discord.ext import commands
from dotenv import load_dotenv


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# DONNEES DU JEU
# =========================================================

players = {}

duel_requests = {}

active_duels = {}

pending_attacks = {}

status_effects = {}

# Joueurs ayant actuellement un sort en attente de résolution
active_casts = set()

# Cooldown offensif
offensive_cooldowns = {}


# =========================================================
# JOUEURS
# =========================================================

def get_player(user):

    if user.id not in players:

        players[user.id] = {

            "name": user.display_name,

            "hp": 100,

            "level": 1,

            "xp": 0,

            # Sorts connus dès le début
            "learned_spells": {
                "confringo",
                "protego",
                "expelliarmus"
            },

            "spellslevel": {
                "confringo": 1,
                "protego": 1,
                "expelliarmus": 1,
                "sectumsempra": 0
            },

            "spellxp": {
                "confringo": 0,
                "protego": 0,
                "expelliarmus": 0,
                "sectumsempra": 0
            }
        }

    return players[user.id]


# =========================================================
# NIVEAU JOUEUR
# =========================================================

def update_player_level(player):

    player["level"] = 1 + player["xp"] // 100


def gain_player_xp(player, amount):

    old_level = player["level"]

    player["xp"] += amount

    update_player_level(player)

    return player["level"] > old_level


# =========================================================
# XP DES SORTS
# =========================================================

def gain_spell_xp(
        player,
        spell_name,
        amount
):

    player["spellxp"][spell_name] += amount

    new_level = (
        1
        + player["spellxp"][spell_name] // 100
    )

    old_level = player["spellslevel"][spell_name]

    player["spellslevel"][spell_name] = max(
        old_level,
        new_level
    )


# =========================================================
# SORT CONNU ?
# =========================================================

def knows_spell(
        player,
        spell_name
):

    return (
        spell_name
        in player["learned_spells"]
    )


# =========================================================
# DESARMEMENT
# =========================================================

def is_disarmed(user_id):

    if user_id not in status_effects:
        return False

    disarmed_until = (
        status_effects[user_id]
        .get("disarmed_until", 0)
    )

    return (
        time.time()
        < disarmed_until
    )


def remaining_disarm_time(user_id):

    if not is_disarmed(user_id):
        return 0

    return max(
        0,
        status_effects[user_id]["disarmed_until"]
        - time.time()
    )


# =========================================================
# COOLDOWNS
# =========================================================

def is_on_cooldown(user_id):

    if user_id not in offensive_cooldowns:
        return False

    return (
        time.time()
        < offensive_cooldowns[user_id]
    )


def remaining_cooldown(user_id):

    if not is_on_cooldown(user_id):
        return 0

    return max(
        0,
        offensive_cooldowns[user_id]
        - time.time()
    )


def start_cooldown(
        user_id,
        duration
):

    offensive_cooldowns[user_id] = (
        time.time() + duration
    )


# =========================================================
# FIN DE RESOLUTION D'UN SORT
# =========================================================

def finish_attack(attack):

    attacker_id = attack["attacker_id"]

    # Le sort n'est plus actif
    active_casts.discard(
        attacker_id
    )

    # IMPORTANT :
    # le cooldown commence APRES la résolution
    start_cooldown(
        attacker_id,
        attack["cooldown"]
    )


# =========================================================
# PUISSANCE
# =========================================================

def calculate_spell_power(
        player,
        spell_name
):

    # Sectumsempra est plus difficile
    # et donc plus instable
    if spell_name == "sectumsempra":

        random_power = random.randint(
            1,
            14
        )

    else:

        random_power = random.randint(
            1,
            20
        )

    return (
        random_power
        + player["level"] * 2
        + player["spellslevel"][spell_name] * 5
    )


# =========================================================
# FIN DU DUEL
# =========================================================

async def check_duel_end(
        ctx,
        loser
):

    loser_player = get_player(
        loser
    )

    if loser_player["hp"] > 0:
        return False

    loser_player["hp"] = 0

    winner_id = active_duels.get(
        loser.id
    )

    if winner_id is None:
        return False

    winner = await bot.fetch_user(
        winner_id
    )

    winner_player = get_player(
        winner
    )

    leveled_up = gain_player_xp(
        winner_player,
        50
    )

    await ctx.send(
        f"🏆 **DUEL TERMINÉ !**\n"
        f"✨ {winner.display_name} remporte le duel contre "
        f"{loser.display_name} !\n"
        f"❤️ {loser.display_name} : **0/100 PV**\n"
        f"⭐ {winner.display_name} gagne **50 XP**."
    )

    if leveled_up:

        await ctx.send(
            f"🌟 **{winner.display_name} passe "
            f"niveau {winner_player['level']} !**"
        )

    # Supprimer le duel
    active_duels.pop(
        loser.id,
        None
    )

    active_duels.pop(
        winner_id,
        None
    )

    # Nettoyer les attaques
    pending_attacks.pop(
        loser.id,
        None
    )

    pending_attacks.pop(
        winner_id,
        None
    )

    # Nettoyer les sorts actifs
    active_casts.discard(
        loser.id
    )

    active_casts.discard(
        winner_id
    )

    # Nettoyer les effets
    status_effects.pop(
        loser.id,
        None
    )

    status_effects.pop(
        winner_id,
        None
    )

    # Nettoyer les cooldowns
    offensive_cooldowns.pop(
        loser.id,
        None
    )

    offensive_cooldowns.pop(
        winner_id,
        None
    )

    return True


# =========================================================
# BACKLASH SECTUMSEMPRA
# =========================================================

async def sectumsempra_backlash(
        ctx,
        attack,
        defense_power
):

    if attack["spell"] != "sectumsempra":
        return

    difference = (
        defense_power
        - attack["power"]
    )

    # Seulement si Sectumsempra est
    # très largement contré
    if difference < 10:
        return

    attacker_id = attack[
        "attacker_id"
    ]

    attacker = await bot.fetch_user(
        attacker_id
    )

    attacker_player = get_player(
        attacker
    )

    damage = random.randint(
        5,
        10
    )

    attacker_player["hp"] -= damage

    if attacker_player["hp"] < 0:
        attacker_player["hp"] = 0

    await ctx.send(
        f"💢 **Le contrôle de Sectumsempra "
        f"se retourne contre {attacker.display_name} !**\n"
        f"🩸 Backlash : **{damage} dégâts**\n"
        f"❤️ {attacker.display_name} : "
        f"**{attacker_player['hp']}/100 PV**"
    )

    await check_duel_end(
        ctx,
        attacker
    )


# =========================================================
# APPLIQUER UNE ATTAQUE
# =========================================================

async def apply_attack(
        ctx,
        defender_user,
        attack
):

    defender = get_player(
        defender_user
    )

    spell = attack["spell"]

    attacker_id = attack[
        "attacker_id"
    ]

    attacker_user = await bot.fetch_user(
        attacker_id
    )

    attacker_player = get_player(
        attacker_user
    )

    # =====================================================
    # CONFRINGO
    # =====================================================

    if spell == "confringo":

        damage = attack[
            "damage"
        ]

        defender["hp"] -= damage

        if defender["hp"] < 0:
            defender["hp"] = 0

        await ctx.send(
            f"🔥 **Confringo touche "
            f"{defender_user.display_name} !**\n"
            f"💥 Dégâts : **{damage}**\n"
            f"❤️ PV : **{defender['hp']}/100**"
        )

        gain_player_xp(
            attacker_player,
            15
        )

        gain_spell_xp(
            attacker_player,
            "confringo",
            15
        )

        await check_duel_end(
            ctx,
            defender_user
        )

    # =====================================================
    # EXPELLIARMUS
    # =====================================================

    elif spell == "expelliarmus":

        duration = attack[
            "duration"
        ]

        status_effects[
            defender_user.id
        ] = {
            "disarmed_until":
                time.time() + duration
        }

        await ctx.send(
            f"⚡ **Expelliarmus touche "
            f"{defender_user.display_name} !**\n"
            f"🪄 {defender_user.display_name} "
            f"est désarmé pendant "
            f"**{duration} secondes** !"
        )

        gain_player_xp(
            attacker_player,
            10
        )

        gain_spell_xp(
            attacker_player,
            "expelliarmus",
            15
        )

    # =====================================================
    # SECTUMSEMPRA
    # =====================================================

    elif spell == "sectumsempra":

        damage = attack[
            "damage"
        ]

        defender["hp"] -= damage

        if defender["hp"] < 0:
            defender["hp"] = 0

        await ctx.send(
            f"🩸 **Sectumsempra frappe "
            f"{defender_user.display_name} !**\n"
            f"💥 Dégâts initiaux : "
            f"**{damage}**\n"
            f"❤️ PV : "
            f"**{defender['hp']}/100**"
        )

        gain_player_xp(
            attacker_player,
            25
        )

        gain_spell_xp(
            attacker_player,
            "sectumsempra",
            20
        )

        finished = await check_duel_end(
            ctx,
            defender_user
        )

        if finished:
            return

        # Premier saignement
        await asyncio.sleep(3)

        if (
            defender_user.id
            not in active_duels
        ):
            return

        defender["hp"] -= 5

        if defender["hp"] < 0:
            defender["hp"] = 0

        await ctx.send(
            f"🩸 {defender_user.display_name} "
            f"continue de saigner : "
            f"**-5 PV**\n"
            f"❤️ PV : "
            f"**{defender['hp']}/100**"
        )

        finished = await check_duel_end(
            ctx,
            defender_user
        )

        if finished:
            return

        # Deuxième saignement
        await asyncio.sleep(3)

        if (
            defender_user.id
            not in active_duels
        ):
            return

        defender["hp"] -= 5

        if defender["hp"] < 0:
            defender["hp"] = 0

        await ctx.send(
            f"🩸 {defender_user.display_name} "
            f"subit encore **5 dégâts de saignement**.\n"
            f"❤️ PV : "
            f"**{defender['hp']}/100**"
        )

        await check_duel_end(
            ctx,
            defender_user
        )


# =========================================================
# LANCER UNE ATTAQUE
# =========================================================

async def launch_attack(
        ctx,
        opponent,
        spell_name,
        power,
        damage=0,
        duration=0,
        cooldown=6
):

    # =====================================================
    # SORT DEJA EN COURS
    # =====================================================

    if ctx.author.id in active_casts:

        await ctx.send(
            "⏳ Ton sort précédent n'est pas encore résolu.\n"
            "Attends la réaction de ton adversaire."
        )

        return

    # =====================================================
    # COOLDOWN
    # =====================================================

    if is_on_cooldown(
        ctx.author.id
    ):

        remaining = remaining_cooldown(
            ctx.author.id
        )

        await ctx.send(
            f"⏳ Tu récupères encore pendant "
            f"**{remaining:.1f} secondes**."
        )

        return

    # L'adversaire doit déjà répondre
    # à une autre attaque
    if opponent.id in pending_attacks:

        await ctx.send(
            f"⚠️ {opponent.display_name} doit déjà "
            f"réagir à une attaque."
        )

        return

    # =====================================================
    # VERROUILLER L'ATTAQUANT
    # =====================================================

    active_casts.add(
        ctx.author.id
    )

    attack_id = time.time_ns()

    pending_attacks[
        opponent.id
    ] = {

        "id":
            attack_id,

        "attacker_id":
            ctx.author.id,

        "spell":
            spell_name,

        "power":
            power,

        "damage":
            damage,

        "duration":
            duration,

        # Le cooldown est stocké ici
        # mais ne commence PAS encore
        "cooldown":
            cooldown
    }

    await ctx.send(
        f"🪄 **{ctx.author.display_name} lance "
        f"{spell_name.upper()} sur "
        f"{opponent.display_name} !**\n"
        f"⏳ {opponent.display_name} a "
        f"**10 secondes** pour réagir.\n"
        f"Réactions possibles : "
        f"`!protego` ou `!expelliarmus`."
    )

    # =====================================================
    # ATTENDRE LA REACTION
    # =====================================================

    await asyncio.sleep(
        10
    )

    # Déjà résolu par une réaction
    if (
        opponent.id
        not in pending_attacks
    ):
        return

    current_attack = pending_attacks[
        opponent.id
    ]

    # Sécurité :
    # vérifier que c'est encore la même attaque
    if (
        current_attack["id"]
        != attack_id
    ):
        return

    del pending_attacks[
        opponent.id
    ]

    # =====================================================
    # RESOLUTION
    # =====================================================

    finish_attack(
        current_attack
    )

    await ctx.send(
        f"⌛ {opponent.display_name} "
        f"n'a pas réagi à temps !\n"
        f"⏳ {ctx.author.display_name} entre maintenant "
        f"en récupération."
    )

    await apply_attack(
        ctx,
        opponent,
        current_attack
    )


# =========================================================
# BOT READY
# =========================================================

@bot.event
async def on_ready():

    print(
        f"Connecté en tant que {bot.user}"
    )


# =========================================================
# TEST
# =========================================================

@bot.command()
async def test(ctx):

    await ctx.send(
        "Le bot fonctionne 🪄"
    )


# =========================================================
# PROFIL
# =========================================================

@bot.command()
async def profil(ctx):

    player = get_player(
        ctx.author
    )

    await ctx.send(
        f"🧙 **{player['name']}**\n"
        f"❤️ PV : {player['hp']}/100\n"
        f"⭐ Niveau : {player['level']}\n"
        f"✨ XP : {player['xp']}\n\n"
        f"🔥 Confringo : niveau "
        f"{player['spellslevel']['confringo']}\n"
        f"🛡️ Protego : niveau "
        f"{player['spellslevel']['protego']}\n"
        f"⚡ Expelliarmus : niveau "
        f"{player['spellslevel']['expelliarmus']}"
    )


# =========================================================
# DUEL
# =========================================================

@bot.command()
async def duel(
        ctx,
        target: discord.Member
):

    if (
        target.id
        == ctx.author.id
    ):

        await ctx.send(
            "Tu ne peux pas te défier toi-même."
        )

        return

    if (
        ctx.author.id in active_duels
        or target.id in active_duels
    ):

        await ctx.send(
            "L'un de vous est déjà en duel."
        )

        return

    duel_requests[
        target.id
    ] = ctx.author.id

    await ctx.send(
        f"⚔️ **{ctx.author.display_name} défie "
        f"{target.display_name} en duel !**\n"
        f"{target.display_name}, utilise "
        f"`!accepter` ou `!refuser`."
    )


# =========================================================
# REFUSER
# =========================================================

@bot.command()
async def refuser(ctx):

    if (
        ctx.author.id
        not in duel_requests
    ):

        await ctx.send(
            "Tu n'as aucune demande "
            "de duel en attente."
        )

        return

    challenger_id = duel_requests[
        ctx.author.id
    ]

    challenger = await bot.fetch_user(
        challenger_id
    )

    del duel_requests[
        ctx.author.id
    ]

    await ctx.send(
        f"❌ {ctx.author.display_name} "
        f"a refusé le duel de "
        f"{challenger.display_name}."
    )


# =========================================================
# ACCEPTER
# =========================================================

@bot.command()
async def accepter(ctx):

    if (
        ctx.author.id
        not in duel_requests
    ):

        await ctx.send(
            "Tu n'as aucune demande "
            "de duel en attente."
        )

        return

    challenger_id = duel_requests[
        ctx.author.id
    ]

    active_duels[
        ctx.author.id
    ] = challenger_id

    active_duels[
        challenger_id
    ] = ctx.author.id

    del duel_requests[
        ctx.author.id
    ]

    challenger = await bot.fetch_user(
        challenger_id
    )

    player1 = get_player(
        challenger
    )

    player2 = get_player(
        ctx.author
    )

    # PV remis à 100
    player1["hp"] = 100
    player2["hp"] = 100

    # Nettoyage du duel précédent
    for user_id in [
        challenger_id,
        ctx.author.id
    ]:

        pending_attacks.pop(
            user_id,
            None
        )

        active_casts.discard(
            user_id
        )

        status_effects.pop(
            user_id,
            None
        )

        offensive_cooldowns.pop(
            user_id,
            None
        )

    await ctx.send(
        f"⚔️ **Duel accepté !**\n"
        f"{challenger.display_name} VS "
        f"{ctx.author.display_name}\n"
        f"❤️ Les deux joueurs commencent "
        f"avec **100 PV**.\n"
        f"🪄 Que le duel commence !"
    )


# =========================================================
# CONFRINGO
# =========================================================

@bot.command()
async def confringo(ctx):

    if (
        ctx.author.id
        not in active_duels
    ):

        await ctx.send(
            "Tu n'es dans aucun duel."
        )

        return

    player = get_player(
        ctx.author
    )

    if is_disarmed(
        ctx.author.id
    ):

        remaining = remaining_disarm_time(
            ctx.author.id
        )

        await ctx.send(
            f"🪄 Tu es désarmé pendant "
            f"encore **{remaining:.1f} secondes**."
        )

        return

    # Confringo n'est pas actuellement
    # autorisé comme réaction
    if (
        ctx.author.id
        in pending_attacks
    ):

        await ctx.send(
            "⚠️ Tu es actuellement attaqué.\n"
            "Tu dois d'abord réagir avec "
            "`!protego` ou `!expelliarmus`."
        )

        return

    opponent_id = active_duels[
        ctx.author.id
    ]

    opponent = await bot.fetch_user(
        opponent_id
    )

    damage = random.randint(
        15,
        30
    )

    power = calculate_spell_power(
        player,
        "confringo"
    )

    await launch_attack(
        ctx,
        opponent,
        "confringo",
        power,
        damage=damage,
        cooldown=6
    )


# =========================================================
# PROTEGO
# =========================================================

@bot.command()
async def protego(ctx):

    if (
        ctx.author.id
        not in pending_attacks
    ):

        await ctx.send(
            "🛡️ Il n'y a aucune attaque "
            "à bloquer."
        )

        return

    defender = get_player(
        ctx.author
    )

    attack = pending_attacks[
        ctx.author.id
    ]

    defense_power = calculate_spell_power(
        defender,
        "protego"
    )

    # =====================================================
    # PROTEGO REUSSI
    # =====================================================

    if (
        defense_power
        >= attack["power"]
    ):

        del pending_attacks[
            ctx.author.id
        ]

        # Le sort est maintenant résolu
        # → cooldown de l'attaquant commence
        finish_attack(
            attack
        )

        await ctx.send(
            f"🛡️ **{ctx.author.display_name} "
            f"lance PROTEGO !**\n"
            f"✨ L'attaque est complètement bloquée !\n"
            f"⚔️ Attaque : **{attack['power']}**\n"
            f"🛡️ Protego : **{defense_power}**\n\n"
            f"⏳ L'attaquant entre maintenant "
            f"en récupération."
        )

        gain_player_xp(
            defender,
            5
        )

        gain_spell_xp(
            defender,
            "protego",
            10
        )

        await sectumsempra_backlash(
            ctx,
            attack,
            defense_power
        )

    # =====================================================
    # PROTEGO RATE
    # =====================================================

    else:

        del pending_attacks[
            ctx.author.id
        ]

        # Même si Protego rate,
        # l'attaque a été résolue
        finish_attack(
            attack
        )

        await ctx.send(
            f"🛡️ **{ctx.author.display_name} "
            f"lance PROTEGO !**\n"
            f"💥 Protego est brisé !\n"
            f"⚔️ Attaque : **{attack['power']}**\n"
            f"🛡️ Protego : **{defense_power}**\n\n"
            f"⏳ L'attaquant entre maintenant "
            f"en récupération."
        )

        await apply_attack(
            ctx,
            ctx.author,
            attack
        )


# =========================================================
# EXPELLIARMUS
# =========================================================

@bot.command()
async def expelliarmus(ctx):

    if (
        ctx.author.id
        not in active_duels
    ):

        await ctx.send(
            "Tu n'es dans aucun duel."
        )

        return

    player = get_player(
        ctx.author
    )

    if is_disarmed(
        ctx.author.id
    ):

        remaining = remaining_disarm_time(
            ctx.author.id
        )

        await ctx.send(
            f"🪄 Tu es désarmé pendant "
            f"encore **{remaining:.1f} secondes**."
        )

        return

    expelliarmus_power = (
        calculate_spell_power(
            player,
            "expelliarmus"
        )
    )

    # =====================================================
    # MODE CONTRE-ATTAQUE
    # =====================================================

    if (
        ctx.author.id
        in pending_attacks
    ):

        incoming_attack = pending_attacks[
            ctx.author.id
        ]

        attacker_id = incoming_attack[
            "attacker_id"
        ]

        attacker = await bot.fetch_user(
            attacker_id
        )

        await ctx.send(
            f"⚡ **{ctx.author.display_name} "
            f"contre avec EXPELLIARMUS !**"
        )

        # =================================================
        # CONTRE REUSSI
        # =================================================

        if (
            expelliarmus_power
            >= incoming_attack["power"]
        ):

            del pending_attacks[
                ctx.author.id
            ]

            # L'attaque adverse est terminée :
            # cooldown de l'ancien attaquant
            finish_attack(
                incoming_attack
            )

            # Désarmement de l'attaquant
            status_effects[
                attacker_id
            ] = {
                "disarmed_until":
                    time.time() + 4
            }

            await ctx.send(
                f"💥 Expelliarmus interrompt "
                f"**{incoming_attack['spell'].upper()}** !\n"
                f"🪄 {attacker.display_name} est "
                f"désarmé pendant **4 secondes** !\n"
                f"⚡ Expelliarmus : "
                f"**{expelliarmus_power}**\n"
                f"🔥 Sort adverse : "
                f"**{incoming_attack['power']}**\n\n"
                f"🎯 **{ctx.author.display_name} "
                f"a maintenant l'initiative !**"
            )

            gain_player_xp(
                player,
                10
            )

            gain_spell_xp(
                player,
                "expelliarmus",
                15
            )

            await sectumsempra_backlash(
                ctx,
                incoming_attack,
                expelliarmus_power
            )

        # =================================================
        # CONTRE RATE
        # =================================================

        else:

            del pending_attacks[
                ctx.author.id
            ]

            finish_attack(
                incoming_attack
            )

            await ctx.send(
                f"❌ Expelliarmus ne parvient pas "
                f"à interrompre "
                f"**{incoming_attack['spell'].upper()}** !\n"
                f"⚡ Expelliarmus : "
                f"**{expelliarmus_power}**\n"
                f"🔥 Sort adverse : "
                f"**{incoming_attack['power']}**"
            )

            await apply_attack(
                ctx,
                ctx.author,
                incoming_attack
            )

        return

    # =====================================================
    # MODE ATTAQUE NORMALE
    # =====================================================

    opponent_id = active_duels[
        ctx.author.id
    ]

    opponent = await bot.fetch_user(
        opponent_id
    )

    await launch_attack(
        ctx,
        opponent,
        "expelliarmus",
        expelliarmus_power,
        duration=4,
        cooldown=6
    )


# =========================================================
# DEMARRER LE BOT
# =========================================================

bot.run(TOKEN)