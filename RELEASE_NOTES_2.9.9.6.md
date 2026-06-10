# GhostShell v2.9.9.6

Two changes — auto-enable game mode on launch, and a much bigger game database.

## 🎮 Game mode now auto-enables on GhostShell boot

The game-detection monitor used to require you to click *Start Monitoring*
in the Profiles tab every time GhostShell launched.  v2.9.9.6 turns that
into a one-line toggle in **Settings → GAME MODE**, ON by default:

> ☑ Auto-enable game mode at GhostShell launch
> Starts the game-detection monitor automatically when GhostShell boots so
> any game you launch afterwards gets the gaming profile applied.

**How it works:**
- Settings persist in `%APPDATA%\GhostShell\game_profiles_settings.json`
- `app.py.main()` calls `game_profiles.auto_enable_on_boot_if_set()` after
  the boot-prep delay (60 s in autostart mode, 0 s in manual launch)
- If the setting is on AND monitoring isn't already running → starts the
  detection loop
- Idempotent — won't restart if you've already started it manually
- Toggle off any time in Settings to go back to manual control

The Settings card also shows live status: whether monitoring is currently
running, what game (if any) is being tracked, and the size of the game
database.

## 📚 Game database expanded to ~830 entries

The database grew from ~470 to **~830** known game executables, adding
roughly 120 lines covering:

**2024–2025 AAA / live-service:**
Indiana Jones and the Great Circle, Avowed, S.T.A.L.K.E.R. 2,
Throne and Liberty, Once Human, Metaphor: ReFantazio, Silent Hill 2 (remake),
Monster Hunter Wilds, Skull and Bones, Star Wars Outlaws, Concord (RIP),
Atomic Heart

**More FPS / shooters:**
Crossfire / CrossfireX / CrossfireHD, Tarkov Arena, Delta Force,
Trepang2, Boundary, Selaco, Prodeus, Metro 2033/Last Light/Exodus, Red Dead 2,
GTA V (incl. FiveM), Saints Row series, Mafia, Watch Dogs 1/2/Legion,
Far Cry 3/4/5/6/New Dawn

**Roguelite / indie picks:**
Risk of Rain 1/2, Vampire Survivors, Enter the Gungeon, Slay the Spire,
Dead Cells, Binding of Isaac, Noita, Cult of the Lamb, Balatro, Brotato,
Backpack Battles, Peglin, DRG: Survivor

**MMO:**
Throne and Liberty, Black Desert, Albion Online, DDO, LotRO, Wakfu, Dofus,
SWTOR, Tibia, RuneScape (incl. RuneLite), EverQuest 1/2, FFXI, Blue Protocol

**Racing sims (recent):**
Le Mans Ultimate, Rennsport, EA WRC, CarX 2, Test Drive Unlimited Solar Crown

**Sports:**
NHL 23/24/25/26, TopSpin 2K25, Tony Hawk's Pro Skater 1+2,
EA Sports PGA Tour, Golf With Friends

**Strategy + sims:**
Victoria 3, Old World, AoS: Realms of Ruin, Heroes of Might and Magic 3 (HotA),
WARNO, Steel Division 2, Regiments, BattleTech, MechWarrior 5, MWO,
Tropico 5/6, KSP 1/2, Caves of Qud, Cogmind

**Survival / sandbox:**
Pacific Drive, Icarus, The Long Dark, SCUM, Stranded Deep, Craftopia,
Wuthering Waves, Tarisland, Aska

**Horror:**
Outlast 1/2 / Trials, Amnesia (all), Callisto Protocol, Dead Space (all),
The First Descendant

**Fighting / brawler:**
Killer Instinct, Skullgirls, KOF XV, Rivals of Aether 2, Samurai Shodown

**Platformers / 3D adventure:**
A Hat in Time, Sonic Frontiers / Generations / Shadow / Superstars,
Rayman series, Crash Bandicoot series, Spyro Reignited, SpongeBob

**Open world / RPG:**
ELEX 1/2, Gothic, Risen, Kingdoms of Amalur, Wo Long: Fallen Dynasty,
Nioh 1/2, Ghost of Yotei, Pentiment, Disco Elysium

**Misc / streamer picks:**
Schedule I, Drug Dealer Simulator, Goat Simulator 3, ULTRAKILL, DUSK,
Amid Evil, Cultic, Another Crab's Treasure

The full list of ~830 entries is in `core/game_profiles.py:KNOWN_GAME_EXES`.

If your favourite game isn't on the list, you can still add it manually in
the **Profiles** tab → *Add Custom Game*.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After upgrading, launch a game (~830 are detected automatically).  The
gaming profile applies the moment GhostShell sees the .exe and reverts
when the game closes — no manual *Start Monitoring* click needed.
