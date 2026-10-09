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
- `database.py`: SQLite JSON player storage, atomic writes, health checks, and safe online backups.

Players begin with `!profile`, choose a house with `!house`, and spend three talent points with `!train`. Challenge another ready player with `!duel @player`; requests expire after two minutes. Accept and continue the duel in the challenge channel. Players are identified globally by Discord user ID, so each player can participate in only one duel across servers.

Attacks give the defender ten seconds to use Protego, Expelliarmus, or Dodge. Offensive recovery starts after resolution. Speed affects recovery and accuracy; Magic Power affects spell strength; Endurance affects HP and shielding; Agility affects dodging. End-of-duel rewards are 50 XP for the winner and 20 XP for the loser.

## Additional spell tuning

These are initial balancing values. Spell levels above one add the listed damage bonus per level. Ordinary six-second recovery has a 4.5-second minimum after Speed adjustment; longer recovery has a ten-second minimum.

| Spell | Direct damage | Per-level bonus | Effect | Base recovery |
|---|---:|---:|---|---:|
| Incendio | 18–26 | 3 | Spell Speed 9; two 4-damage burn ticks, three seconds apart | 6 seconds |
| Diffindo | 26–36 | 4 | Spell Speed 8 pressures dodging | 6 seconds |
| Depulso | 15–22 | 3 | Stun for 2 seconds | 6 seconds |
| Glacius | 12–18 | 3 | Reduce Speed by 5 for 10 seconds | 6 seconds |
| Petrificus Totalus | 6–10 | 2 | Stun for 4 seconds | 12 seconds |
| Bombarda | 38–52 | 4 | Heavy direct damage; 25% passes successful Protego | 15 seconds |
| Endoloris | 0 | 0 | 14 damage at 3s, 6s, and 9s after a hit | 15 seconds |
| Impero | 0 | 0 | Force the victim's highest-level eligible offensive spell against themselves | 15 seconds |

Successful Protego blocks prevent spell effects, but Bombarda and Sectumsempra still deal 25% of their rolled direct damage, rounded down. This partial damage is saved and can end the duel normally. Sectumsempra bleeding is prevented, and its existing backlash remains if the defender survives. Successful Dodge and Expelliarmus counters still prevent all incoming damage. Partial damage alone does not grant attacker hit/XP credit; the defender keeps their successful-block credit. Avada Kedavra retains its existing one-third initial success chance, five-completed-duel availability, and ordinary defensive reactions.

## Curse learning and forced casts

Endoloris and Impero are difficulty-four learnable spells (10 questions, 8 correct answers required before bonuses). Both require player Level 5, 10 successful control spells, and 5 duel wins. Endoloris additionally requires Stupefy Spell Level 3; Impero requires Petrificus Totalus Spell Level 2. Ravenclaw's existing requirement and quiz bonuses apply. Both curses have Spell Speed 7 and 15-second base offensive recovery.

Both curses make an initial hidden d6 roll: Endoloris proceeds on 3–6 (2/3 chance), and Impero proceeds on 4–6 (1/2 chance). A miss starts normal Speed-adjusted recovery without creating a pending attack or granting hit XP. Successful rolls still allow the ordinary 10-second defense window. A forced Endoloris cast also makes this roll.

Petrificus Totalus requires player Level 4, Stupefy Spell Level 2, 5 successful control spells, and 3 duel wins (before Ravenclaw adjustments).

Endoloris causes no immediate damage and does not stun or disarm. Each of its three ticks saves HP and checks normal duel completion. Separate successful casts stack independently, like the existing delayed damage effects. Ticks are scheduled relative to the hit, preventing message latency from shifting the 3s/6s/9s deadlines.

Impero itself causes no direct damage. Its victim's highest-level learned offensive payload is selected, with random selection among ties. Eligible payloads are Confringo, Expelliarmus, Stupefy, Incendio, Diffindo, Depulso, Glacius, Petrificus Totalus, Bombarda, Sectumsempra, and Endoloris. Protego is defensive, Impero is a command curse (excluded to avoid recursion), and Avada Kedavra is never eligible.

The forced payload uses the victim's stats, spell level, spell speed, damage formula, effects, and ordinary offensive recovery. It bypasses voluntary casting gates (cooldown, stun, disarm, and existing cast) because it is compelled. It creates a normal pending attack against that same victim, with a fresh 10-second reaction window. Normal defense eligibility and rolls apply: Protego, Expelliarmus, or Dodge. A successful self-counter disarms the victim, and Sectumsempra backlash affects the victim, because they are the payload's caster. Spell hits and XP follow existing caster attribution; the original Impero caster receives the curse's hit/control credit, and the victim receives any ordinary forced-payload casting credit. Fatal self-damage awards the duel to the other participant through normal duel completion.

Retired spells are pruned from saved learned spells, spell levels, spell XP, and hit counters when profiles are loaded. Live learning trials are discarded at restart. Talent points and unrelated profile data are preserved.

## Normal duel inactivity

A normal PvP duel ends after **300 consecutive seconds** without an accepted player combat action. Both participants share one last-activity timestamp and one scheduled deadline. Valid offensive attempts (including a failed initial curse roll), Protego, Dodge, and Expelliarmus counters reset the deadline, even when a defense roll fails. Unavailable/invalid casts, unrelated commands, ordinary Discord messages, delayed damage ticks, and automatically forced self-casts do not reset it.

At the deadline the duel ends without a winner or loser. Each player loses 10 XP, clamped at zero; player Level is recalculated and may decrease, while existing Talent Points are kept. Wins, losses, completed duels, Avada recharge counters, and normal completion rewards are unchanged. Both players return to their current maximum HP. Pending attacks, active casts, cooldowns, statuses, and the scheduled timer are cleared; delayed combat work is invalidated by the old session identifier. The duel channel receives an inactivity announcement.

A valid action accepted **before** the deadline postpones expiration. At or after the deadline, new actions are rejected and timeout cleanup proceeds. Cleanup claims the session and applies penalties without yielding, so competing callbacks cannot penalize the same duel twice. A normal victory stops its timer. Each simultaneous duel has its own deadline, and Training Duels have no inactivity timer or XP penalty.

Inactivity is tracked only in memory. Restart expires live sessions without rewards or penalties; an ordinary gateway reconnect preserves live sessions and their existing deadlines.

## Cooperative Training Duels

A teacher offers `!teach @student <spell>`, or a student requests `!askhelp @teacher <spell>`. In either direction the roles remain teacher and student; the other player answers `!accept` or `!decline` in the request channel. Requests expire after two minutes. Only one teaching request can involve a player at a time, and pending normal duel requests must be resolved first. The original normal `!duel`, `!accept`, and `!decline` flow remains available. If both kinds of requests exist in an old/ambiguous inbox, use `!accept training` or `!accept duel` (likewise for `!decline`).

The teacher must know the target spell at Spell Level 3 or above. The student must not know it yet. Both characters must be ready, free of active combat and Knowledge Trials, and available in the request server. The student must already meet all non-practical prerequisites: player Level, minimum stats, required learned spells, prerequisite Spell Levels, normal duel wins, and any requirement to learn all other spells. These checks run when requesting and again on acceptance.

Training uses the existing combat engine, spell mechanics, cooldowns, and reactions in the request channel. Participants start at full HP and return to their pre-training HP when it ends. `!training` displays current objectives. Either player can use `!canceltraining` from any channel to leave or cancel a pending teaching request.

Objectives are generated only from unmet `spell_hits` and `combat_stats` requirements. The remaining target subtracts the student's permanent progress and uses the student's Ravenclaw adjustments. Example: an ordinary five-hit requirement with two existing hits becomes three training hits. Requirements already satisfied through normal play or saved training are omitted. Spells with no outstanding practical objectives use the existing Knowledge Trial directly and do not create an empty training session.

Each objective tracks teacher and student contributions separately. Combined contributions must reach its target, and the student must personally succeed at least once in **each** objective. One Confringo hit can correctly advance both its specific hit objective and a successful-attack objective once each. Unrelated players and sessions cannot contribute. Progress summaries appear after relevant successful actions; completed objectives do not accumulate further live counts.

Training grants no player XP, spell XP, spell levels, Talent Points, permanent combat stats, permanent spell hits, duel wins/losses, duel completion counts, or normal duel rewards. The teacher's actions never increase the student's lifetime counters. Avada Kedavra keeps its usual availability check, initial roll, and combat behavior, but training attempts do not consume or recharge its completed-duel availability. Owner/admin progression commands remain available.

An objective is saved to the student's `practical_training` profile field as soon as its numeric target and personal-contribution rule are both met. Full completion saves the spell's practical certificate, ends training, and makes the ordinary `!learn <spell>` Knowledge Trial available, provided non-practical prerequisites are still met. It does **not** teach the spell automatically or reduce the quiz passing score. Quiz difficulty and Ravenclaw's quiz bonus remain unchanged. The former one-answer teaching discount has been removed.

Cancellation, zero HP, detected server departure, channel deletion, bot removal from the server, or a command failure safely ends training. Completed objectives remain saved and are omitted on a subsequent session; incomplete live counts reset. Restart also discards the live session and pending teaching requests, preserving completed objectives/certificates and pre-training HP. Delayed effects and pending attacks from an ended session cannot affect a later duel.

Offline status or a Discord client disconnect is not reliably detectable with the current intents. Server-departure cleanup runs when Discord delivers member-removal events; receiving these may require enabling the Members intent in the deployment. No additional privileged intents are enabled by this feature. Use `!canceltraining` when leaving voluntarily.

## Persistence and restart behavior

Permanent profiles/progression and completed practical objectives/certificates use the existing JSON `players` table. The absolute database path is always `players.db` beside `database.py`, including when launched from another directory. Existing records receive missing defaults and legacy spell-name migrations when loaded; corrupt records are logged and refused rather than replaced. WAL, FULL synchronization, a bounded five-second busy timeout, and transaction-managed connections protect writes. Both profiles are written in one transaction for normal duel results and inactivity penalties.

Live duels, teaching/challenge requests, attacks, casts, statuses, cooldowns, and Knowledge Trials are memory-only. On startup, legacy runtime snapshots are cleared; no combat resumes and no restart XP penalty/reward is given. Training stores pre-session HP, not training damage. New duels always start at full HP. Normal HP already saved before a hard crash can remain visible in profiles until the next duel; it cannot resume damage/status effects. Completed learning/training progression remains saved. A gateway reconnect is not a restart and does not erase live sessions.

Delayed tasks are tracked by unique session ID, validate current sessions, and are cancelled on combat end. Cleanup occurs before duel-result announcements, so Discord send failures cannot strand completed duels or award them twice. SIGINT/SIGTERM abort active sessions without rewards/penalties, restore HP, and cancel background work. A process lock prevents duplicate launches from this application directory. Run exactly one instance; copies in different directories still need operator coordination.

See [DEPLOYMENT.md](DEPLOYMENT.md) for configuration, backups, permissions, hosting, and remaining limitations. No gameplay balance values changed for deployment preparation.

## Tests

Run `python -B -m unittest discover -s tests -v`. Tests use temporary databases and mocked Discord objects, without connecting to Discord or changing `players.db`.

## Duellium rankings

Every normal completed PvP duel is ranked; there is no casual mode. In the duel's server, the winner gains 10 player points, the loser loses up to 3 points (never below zero), and the winner's current stored House gains 10 House Points. The losing House loses nothing. Training, inactivity, cancellation, rejected challenges, and incomplete duels grant no ranking points. Existing XP and progression rewards are unchanged.

- `!leaderboard`: current server's top 10 players; shows your local rank when outside the top 10.
- `!globalleaderboard`: top 10 by the SQL sum of local points across all servers; shows your global rank when outside the top 10. Also works in DMs.
- `!houseleaderboard`: the current server's four Houses, including zero-point Houses.

Player ties use ascending Discord user ID; House ties use alphabetical House key order. Players without a ranked result are unranked with zero points. Names use guild/cache display names, then the saved global profile name, then a safe ID fallback. Looking at a leaderboard does not create or modify profiles or points. Guild-local commands require a server channel; the existing active-duel channel rule remains in force.

SQLite adds `local_player_points` keyed by `(guild_id, user_id)`, `local_house_points` keyed by `(guild_id, house)`, and `ranked_duel_results` keyed by the unique duel session ID. Local player scores have indexed ordering and user lookup. Global totals are queried using `SUM(points) GROUP BY user_id`; no duplicate global total or global House score is stored. Normal duel completion saves existing profile rewards, both local player changes, House points, and the result receipt in one transaction. The in-memory ending guard and persistent unique receipt prevent repeat awards, including after restart. Backups automatically include these tables. Existing profiles are not duplicated per guild, migrated into rankings, or awarded retroactive points.

To test with two accounts: finish both profiles, use `!duel @opponent` / `!accept`, and complete the combat normally. Check the local player and House boards. Reverse the winner to test the zero floor. Play in a second server to check local isolation and global sums; restart and check persistence. A five-minute inactive duel and a teaching/training session should leave all ranking scores unchanged. New migration tables are created on the next normal startup; the existing database is not wiped.
