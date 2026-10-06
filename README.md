# Harry Potter Discord Combat Bot

A Python Discord bot for wizard profiles, timed duels, spell progression, and multiple-choice learning trials. Commands use the `!` prefix; use `!help` for command usage.

## Run

Use Python 3.9 or newer. Install dependencies with `python -m pip install -r requirements.txt`. Create a `.env` beside `bot.py` containing `DISCORD_TOKEN` and `BOT_OWNER_ID` (the owner's numeric Discord user ID). Enable the Message Content Intent for the bot in the Discord developer portal, then run `python bot.py`.

The bot validates configuration before connecting. Importing the module for tests does not initialize the database or connect to Discord. The SQLite file is located beside `database.py`, independently of the working directory.

## Architecture

- `bot.py`: Discord commands, player progression, duel lifecycle, and learning sessions.
- `combat.py`: Discord-independent numeric tuning for the additional spells.
- `spells.py`: Spell metadata, prerequisites, and quiz thresholds.
- `quiz_questions.py`: Multiple-choice question bank.
- `database.py`: SQLite JSON player storage and resumable runtime snapshots.

Players begin with `!profile`, choose a house with `!house`, and spend three talent points with `!train`. Challenge another ready player with `!duel @player`; requests expire after two minutes. Accept and continue the duel in the challenge channel. Players are identified globally by Discord user ID, so each player can participate in only one duel across servers.

Attacks give the defender ten seconds to use Protego, Expelliarmus, or Dodge. Offensive recovery starts after resolution. Speed affects recovery and accuracy; Magic Power affects spell strength; Endurance affects HP and shielding; Agility affects dodging. End-of-duel rewards are 50 XP for the winner and 20 XP for the loser.

## Additional spell tuning

These are initial balancing values. Spell levels above one add the listed damage bonus per level. Ordinary six-second recovery has a 4.5-second minimum after Speed adjustment; longer recovery has a ten-second minimum.

| Spell | Direct damage | Per-level bonus | Effect | Base recovery |
|---|---:|---:|---|---:|
| Incendio | 10–18 | 2 | Spell Speed 9; two 3-damage burn ticks, three seconds apart | 6 seconds |
| Diffindo | 18–28 | 3 | Spell Speed 8 pressures dodging | 6 seconds |
| Depulso | 8–14 | 2 | Stun for 2 seconds | 6 seconds |
| Glacius | 6–12 | 2 | Reduce Speed by 5 for 10 seconds | 6 seconds |
| Petrificus Totalus | 0–3 | 1 | Stun for 4 seconds | 12 seconds |
| Bombarda | 30–42 | 3 | Heavy direct damage; 25% passes successful Protego | 15 seconds |
| Endoloris | 0 | 0 | 10 damage at 3s, 6s, and 9s after a hit | 15 seconds |
| Impero | 0 | 0 | Force the victim's highest-level eligible offensive spell against themselves | 15 seconds |

Successful Protego blocks prevent spell effects, but Bombarda and Sectumsempra still deal 25% of their rolled direct damage, rounded down. This partial damage is saved and can end the duel normally. Sectumsempra bleeding is prevented, and its existing backlash remains if the defender survives. Successful Dodge and Expelliarmus counters still prevent all incoming damage. Partial damage alone does not grant attacker hit/XP credit; the defender keeps their successful-block credit. Avada Kedavra retains its existing one-third initial success chance, five-completed-duel availability, and ordinary defensive reactions.

## Curse learning and forced casts

Endoloris and Impero are difficulty-four learnable spells (10 questions, 8 correct answers required before bonuses). Both require player Level 5, 10 successful control spells, and 5 duel wins. Endoloris additionally requires Stupefy Spell Level 3; Impero requires Petrificus Totalus Spell Level 2. Ravenclaw's existing requirement and quiz bonuses apply. Both curses have Spell Speed 7 and 15-second base offensive recovery.

Both curses make an initial hidden d6 roll: Endoloris proceeds on 3–6 (2/3 chance), and Impero proceeds on 4–6 (1/2 chance). A miss starts normal Speed-adjusted recovery without creating a pending attack or granting hit XP. Successful rolls still allow the ordinary 10-second defense window. A forced Endoloris cast also makes this roll.

Petrificus Totalus requires player Level 4, Stupefy Spell Level 2, 5 successful control spells, and 3 duel wins (before Ravenclaw adjustments).

Endoloris causes no immediate damage and does not stun or disarm. Each of its three ticks saves HP and checks normal duel completion. Separate successful casts stack independently, like the existing delayed damage effects. Ticks are scheduled relative to the hit, preventing message latency from shifting the 3s/6s/9s deadlines.

Impero itself causes no direct damage. Its victim's highest-level learned offensive payload is selected, with random selection among ties. Eligible payloads are Confringo, Expelliarmus, Stupefy, Incendio, Diffindo, Depulso, Glacius, Petrificus Totalus, Bombarda, Sectumsempra, and Endoloris. Protego is defensive, Impero is a command curse (excluded to avoid recursion), and Avada Kedavra is never eligible.

The forced payload uses the victim's stats, spell level, spell speed, damage formula, effects, and ordinary offensive recovery. It bypasses voluntary casting gates (cooldown, stun, disarm, and existing cast) because it is compelled. It creates a normal pending attack against that same victim, with a fresh 10-second reaction window. Normal defense eligibility and rolls apply: Protego, Expelliarmus, or Dodge. A successful self-counter disarms the victim, and Sectumsempra backlash affects the victim, because they are the payload's caster. Spell hits and XP follow existing caster attribution; the original Impero caster receives the curse's hit/control credit, and the victim receives any ordinary forced-payload casting credit. Fatal self-damage awards the duel to the other participant through normal duel completion.

Retired spells are pruned from saved learned spells, spell levels, spell XP, and hit counters when profiles are loaded. Retired learning trials and teaching bonuses are discarded during runtime restoration. Talent points and unrelated profile data are preserved.

## Persistence and restart behavior

Player profiles and HP are saved in SQLite. Runtime snapshots preserve duel participants and channel, challenges, learning progress, teaching bonuses, statuses, and offensive cooldowns. Commands save snapshots when completed; launching an attack also saves before its reaction timer.

On restart, duels and quizzes resume. In-flight attacks and remaining burn/bleed/curse ticks are cancelled, and restored duel channels receive a notice. Already-applied damage remains. Status and cooldown timestamps continue to expire during downtime. Delayed damage checks a unique duel identifier, preventing effects from leaking into a subsequent duel.

Runtime snapshots and individual profile saves are separate transactions; they do not provide atomic recovery from a crash during a command. Run one bot process per database. SQLite operations are synchronous and intended for this small bot's workload.

Owner commands use a shared authorization check against `BOT_OWNER_ID`. Invalid arguments receive usage guidance; unexpected failures are logged and receive a short Discord error response.

## Tests

Run `python -B -m unittest discover -s tests -v`. Tests use temporary databases and mocked Discord objects, without connecting to Discord or changing `players.db`.
