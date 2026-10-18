"""Game Profile Engine — broad detection, hard tweaks on game, full revert to normal.

State management:
- On first run, captures the system's "normal" baseline settings to a JSON file.
- When a game is detected, applies hard gaming tweaks and writes a "gaming_active" flag.
- When the game exits, reverts ALL tweaks to the saved baseline.
- On startup, checks if the "gaming_active" flag is set (meaning a previous session crashed
  or rebooted mid-game) and automatically reverts to normal mode.
"""
import json
import os
import re
import time
import threading
from config import APP_NAME, APPDATA_DIR, OWN_PROCESS_NAMES
from core import process_tuning, reg_originals
from core.network_tweaks import qos_policy_filter
from core.utils import run_ps, run_cmd, get_logger, atomic_write_json, is_safe_ps_exe_name

log = get_logger("profiles")

try:
    from core import notifier as _notifier
except Exception:
    _notifier = None

try:
    from core import cleaner as _cleaner
except Exception:
    _cleaner = None

PROFILES_DIR = os.path.join(APPDATA_DIR, "game_profiles")
os.makedirs(PROFILES_DIR, exist_ok=True)

BASELINE_FILE = os.path.join(PROFILES_DIR, "normal_baseline.json")
GAMING_FLAG_FILE = os.path.join(PROFILES_DIR, "gaming_active.flag")

# Power-plan settings changed for the length of a game session.  Each one's
# previous value is noted first (core/reg_originals.py, under this scope) —
# per power plan, so it goes back onto the plan it was changed on even when a
# per-game target switched plans in between.
_SCOPE = "gamemode"
_PROC_SUB = "54533251-82be-4824-96c1-47b60b740d00"      # processor power management
_CORE_PARKING_MIN = (                                   # minimum share of cores kept unparked, per core type
    "0cc5b647-c1df-4637-891a-dec35c318583",             #   efficiency class 0: E-cores (every core on a non-hybrid CPU)
    "0cc5b647-c1df-4637-891a-dec35c318584",             #   efficiency class 1: P-cores
)

# ═══════════════════════════════════════════════════════════════
# Known game executables (300+)
# ═══════════════════════════════════════════════════════════════
KNOWN_GAME_EXES = {
    # FPS / Shooters
    "valorant-win64-shipping.exe","valorant.exe","cs2.exe","csgo.exe",
    "fortniteclient-win64-shipping.exe","fortnitelauncher.exe","fortnite.exe",
    "r5apex.exe","r5apex_dx12.exe","overwatch.exe","overwatch2.exe",
    "cod.exe","blackops6.exe","modernwarfare3.exe","cod23-cod.exe",
    "modernwarfare2.exe","blackopscoldwar.exe","warzone.exe","modernwarfare.exe",
    "rainbow6.exe","rainbowsix.exe","rainbowsix_be.exe","rainbowsix_vulkan.exe",
    "destiny2.exe","pubg.exe","tslgame.exe","tslgame_be.exe",
    "bf2042.exe","bf1.exe","bfv.exe","bf4.exe","bf3.exe","bf2.exe","bf2142.exe",
    "haloinfinite.exe","mcc-win64-shipping.exe","halo5forge.exe",
    "insurgencysandstorm.exe","insurgency.exe",
    "squad.exe","tarkov.exe","escapefromtarkov.exe","eftarena.exe",
    "deadlock.exe","helldivers2.exe","xdefiant.exe","thefinals.exe",
    "deltaforcegame.exe","marvelrivals.exe","marvelrivals-win64-shipping.exe","marvel-win64-shipping.exe",
    "quake.exe","quakechampions.exe","quakelive.exe","diabotical.exe",
    "doometernal.exe","doomx64.exe","doom_x64vk.exe","doom2016.exe",
    "wolfenstein.exe","wolfenstein2.exe","newcolossus_x64vk.exe",
    "titanfall2.exe","titanfall.exe","apex.exe","apexlegends.exe",
    "splitgate.exe","splitgate2.exe","paladins.exe",
    "hunt.exe","huntshowdown.exe","huntgame.exe",
    "readyornot.exe","groundbranch-win64-shipping.exe","takeonhelicopters.exe",
    "starcitizen.exe","starcitizenlauncher.exe",
    "warthunder.exe","aces.exe","worldoftanks.exe","wargaming.net game center.exe",
    "worldofwarships.exe","worldofwarplanes.exe",
    "planetside2.exe","arma3.exe","arma3_x64.exe","arma3launcher.exe",
    "payday2_win32_release.exe","payday3.exe",
    # Battle Royale
    "naraka-win64-shipping.exe","naraka.exe",
    # MOBA
    "league of legends.exe","leagueclient.exe","leagueclientux.exe",
    "dota2.exe","smite.exe","heroesofthestorm_x64.exe","hots.exe",
    "wildrift.exe","pokemonunite.exe",
    # MMO / RPG
    "ffxiv_dx11.exe","ffxiv.exe","wow.exe","wowclassic.exe","retail.exe",
    "gw2-64.exe","gw2.exe","eso64.exe","eso.exe","newworld.exe","lostark.exe",
    "eldenring.exe","darksouls3.exe","darksoulsremastered.exe","darksoulsii.exe",
    "darksoulsiii.exe","sekiro.exe","bloodborne.exe",
    "baldursgate3.exe","bg3.exe","bg3_dx11.exe","bg3_vulkan.exe",
    "starfield.exe","cyberpunk2077.exe","witcher3.exe","thewitcher3.exe",
    "skyrimse.exe","skyrim.exe","fallout4.exe","fallout76.exe","falloutnv.exe","fallout3.exe",
    "hogwartslegacy.exe","diablo4.exe","diablo3.exe","diablo2.exe","diabloii.exe",
    "pathofexile.exe","pathofexile_x64.exe","pathofexile_x64steam.exe",
    "pathofexile2.exe","poe2.exe","pathofexile2_x64.exe",
    "palworld-win64-shipping.exe","palworld.exe",
    "divinityoriginalsin2.exe","divinity2.exe","dos2.exe",
    "fable3.exe","fableanniversary.exe","fable-unsorted.exe",
    "kingdomcomedeliverance.exe","kcd.exe","kingdomcomedeliverance2.exe",
    "darktide.exe","vermintide2.exe","warhammervermintide.exe","spacemarine2.exe",
    "monster hunter rise.exe","monsterhunterrise.exe","mhrise.exe",
    "monsterhunterworld.exe","mhw.exe","monster hunter wilds.exe",
    "dragonsdogma2.exe","dragonsdogma.exe","dd2.exe",
    "persona5royal.exe","persona3reload.exe","persona4golden.exe",
    # Racing
    "forzahorizon5.exe","forzahorizon4.exe","forzahorizon3.exe","forzamotorsport.exe",
    "assettocorsa.exe","acs.exe","assettocorsacompetizione.exe","acc.exe",
    "f1_23.exe","f1_24.exe","f1_25.exe","f1_22.exe","iracingsim64dx11.exe",
    "iracingui.exe","iracing.exe",
    "nfs-unbound.exe","needforspeed.exe","nfs_heat.exe","nfs_payback.exe",
    "dirt5.exe","dirtrally2.exe","wreckfest.exe","wreckfest2.exe",
    "beamng.x64.exe","beamng.x86.exe","beamng.drive.exe","mycareer.exe",
    "carx.exe","carxdriftracingonline.exe","carxstreet.exe","carstreet.exe",
    "ridebkt.exe","ride4.exe","thecrew.exe","thecrewmotorfest.exe","thecrew2.exe",
    "gt7.exe","grid.exe","grid2.exe","gridautosport.exe","gridlegends.exe",
    "projectcars2.exe","projectcars3.exe","automobilista2.exe","rfactor2.exe",
    # Sports
    "fc24.exe","fc25.exe","fc26.exe","fifa23.exe","fifa22.exe","fifa21.exe",
    "nba2k24.exe","nba2k25.exe","nba2k26.exe","madden24.exe","madden25.exe","madden26.exe",
    "mlbtheshow.exe","mlb.exe","pga2k25.exe","pga2k23.exe","ea sports ufc 5.exe",
    "efootball.exe","pes2021.exe",
    # Strategy
    "stellaris.exe","hoi4.exe","eu4.exe","eu5.exe","ck3.exe","ck2.exe","imperator.exe",
    "totalwar.exe","warhammer3.exe","warhammer2.exe","rome2.exe","threekingdoms.exe",
    "pharaoh_dynasties.exe","troy.exe",
    "civilizationvi.exe","civ6.exe","civilizationvii.exe","civ7.exe",
    "citiesskylines2.exe","citiesskylines.exe","cities.exe",
    "factorio.exe","aoe2de_s.exe","aoe4.exe","aoe1de_s.exe","aomretold.exe",
    "anno1800.exe","anno117.exe","anno1404.exe","anno2070.exe",
    "stardewvalley.exe","manorlords.exe","manor_lords.exe","xcom2.exe","xcom.exe",
    "bannerlord.exe","mount.and.blade2.exe","mb2bannerlord.exe",
    "companyofheroes3.exe","coh3.exe","coh2.exe","coh.exe",
    "frostpunk.exe","frostpunk2.exe","satisfactory-winx64-shipping.exe",
    "oxygen not included.exe","rimworldwin64.exe","rimworld.exe",
    # Survival
    "rust.exe","rustclient.exe","javaw.exe","minecraft.exe","minecraftlauncher.exe",
    "minecraftlauncher.exe","launcher.exe",
    "arkascended.exe","shootergame.exe","arksurvivalascended.exe","arkse.exe",
    "dayz.exe","dayz_x64.exe","dayz_be.exe","valheim.exe","valheim_be.exe",
    "7daystodie.exe","theforest.exe","sonsoftheforest.exe","son-win64-shipping.exe",
    "satisfactory.exe","subnautica.exe","grounded.exe","enshrouded.exe","enshrouded-win64-shipping.exe",
    "nightingale.exe","sosurvival.exe","conan.exe","conansandbox.exe",
    "projectzomboid64.exe","projectzomboid32.exe","pz.exe",
    "lastepoch.exe","vrising.exe","vrisingserver.exe","soulmask.exe","smallland.exe",
    "corekeeper.exe","raft.exe","green hell.exe",
    # Horror / Coop
    "phasmophobia.exe","deadbydaylight-win64-shipping.exe","deadbydaylight.exe",
    "lethalcompany.exe","contentwarning.exe","contentwarning-win64-shipping.exe",
    "repo.exe","repo-win64-shipping.exe","r.e.p.o.exe",
    "residentevil4.exe","re4.exe","residentevil8.exe","re8.exe","residentevil2.exe",
    "residentevil3.exe","residentevilvillage.exe","revelations2.exe",
    "thedarkpictures.exe","thequarry.exe","untildawn.exe","thecasting.exe",
    "alanwake2.exe","alanwakeremastered.exe","alanwake.exe",
    # Fighting
    "streetfighter6.exe","sf6.exe","tekken8.exe","tekken7.exe",
    "mortalkombat1.exe","mk1.exe","mortalkombat11.exe","mk11.exe","mortalkombatx.exe",
    "guiltygearstrive.exe","ggst-win64-shipping.exe","dnfduel.exe","dragonball sparking zero.exe",
    # Indie / Other
    "rocketleague.exe","fallguys_client.exe","fallguys.exe","terraria.exe",
    "hades2.exe","hades.exe","hollowknight.exe","celeste.exe","hollow_knight_silksong.exe",
    "cuphead.exe","deeprockgalactic.exe","nomansky.exe",
    "godofwar.exe","godofwarragnarok.exe","spiderman.exe","spiderman2.exe","spidermanremastered.exe",
    "ghostoftsushima.exe","horizonzerodawn.exe","horizonforbiddenwest.exe",
    "thelastofus-part1.exe","thelastofus-part2.exe","tlouii.exe","tlou2.exe","uncharted4.exe",
    "remnant2.exe","remnant.exe",
    "warframe.exe","warframe.x64.exe","warframex64.exe",
    "genshinimpact.exe","yuanshen.exe","genshinimpactlauncher.exe",
    "honkaistarrail.exe","starrail.exe","honkaiimpact3.exe","honkai impact 3rd.exe",
    "zenlesszonezero.exe","wutheringwaves.exe","wuwa.exe","client-win64-shipping.exe",
    "expedition33.exe","clair_obscur.exe","clair obscur expedition 33.exe",
    "kingdomhearts3.exe","kh3.exe","ffvii_remake.exe","ff16_game.exe","finalfantasy7rebirth.exe",
    "disneydreamlightvalley.exe","stellarblade.exe","senuassaga.exe","hellblade2.exe",
    "blackmythwukong.exe","b1-win64-shipping.exe","wukong.exe",
    "inzoi.exe","sims4_x64.exe","sims4.exe","thesims4.exe",
    "deathstranding.exe","deathstranding2.exe","ds2.exe",
    "returnal.exe","returnal-win64-shipping.exe","sifu.exe","absolver.exe",
    "nier.exe","nierautomata.exe","nierreplicant.exe","nierautomata-win64-shipping.exe",
    "darksoulsiii.exe","lordsofthefallen2.exe","lords of the fallen.exe","lotf2.exe",
    "stellarblade.exe","wildheartsalpha.exe","wildhearts.exe",
    # Emulators
    "rpcs3.exe","yuzu.exe","ryujinx.exe","ryujinx.ava.exe","cemu.exe","cemu_hook.exe",
    "dolphin.exe","pcsx2.exe","pcsx2-qt.exe","ppsspp.exe","ppsspp64.exe",
    "retroarch.exe","retroarch.exe","xenia.exe","xenia_canary.exe",
    "suyu.exe","citra-qt.exe","mgba.exe","snes9x.exe","vba.exe","nestopia.exe",
    "duckstation-qt-x64-releaselto.exe","duckstation.exe","melonds.exe",
    "project64.exe","mupen64plus.exe","fpse.exe",
    # VR
    "vrchat.exe","boneworks.exe","bonelab.exe","beatsaber.exe","pavlov.exe",
    "blade and sorcery.exe","bladeandsorcery.exe","halflife-vr.exe","halflifealyx.exe",
    "boneworks-win64-shipping.exe","rec.room.exe","recroom.exe","phasmophobia-vr.exe",
    "alyx.exe","noclipgame.exe","contractorsshowdown.exe","contractors.exe",
    "ghostsofcapelos.exe","walkabout minigolf.exe",
    # Launchers that run heavy games (not the store, the launcher wrapping a game)
    # These we want detected because players sometimes launch games without them hitting our list
    # (but NOT the store clients — those stay in IGNORE)

    # ─── v2.9.9.6 — expanded game list (≈120 more entries) ─────────────────
    # 2024-2025 AAA / live-service
    "indianajones.exe","indianajonesandthegreatcircle.exe","tge-win64-shipping.exe",
    "avowed.exe","avowed-wingdk-shipping.exe","stalker2.exe","stalker2-win64-shipping.exe",
    "thronedandliberty.exe","tl.exe","oncehuman.exe",
    "metaphorrefantazio.exe","metaphor.exe",
    "silenthill2.exe","sh2.exe","silenthill2remake.exe",
    "mhwilds.exe","mhwilds-win64-shipping.exe","monsterhunterwilds.exe",
    "skullandbones.exe","sab.exe",
    "alone in the dark.exe","aloneinthedark.exe",
    "dragonsdogma2.exe","dd2.exe",
    "starwarsoutlaws.exe","massive.exe",
    "concordretail.exe","concord.exe",
    "robocop.exe","robocoprogue.exe","rrc.exe",
    "atomicheart.exe","atomic_heart.exe","atomic-win64-shipping.exe",
    "tek.exe","tekkenrevolution.exe","tekken8-win64-shipping.exe",

    # More FPS / shooters
    "crossfire.exe","crossfirex.exe","crossfirehd.exe",
    "tarkov-arena.exe","arena.exe","escapefromtarkov-arena.exe",
    "themostatk.exe","themost-win64-shipping.exe",
    "delta_force.exe","dfgame.exe","deltaforce-win64-shipping.exe",
    "trepang2.exe","trepang2-win64-shipping.exe",
    "boundary.exe","boundary-win64-shipping.exe",
    "syndicate.exe","prodeus.exe","selaco.exe",
    "ironsight.exe","metroexodus.exe","metro2033.exe","metroredux.exe",
    "cyberpunk_phantomliberty.exe","redm.exe","rdr2.exe","reddeadredemption2.exe",
    "gta5.exe","grandtheftautov.exe","gtav.exe","fivem.exe","fivem_b2802.exe","fivem_gtaprocess.exe",
    "saintsrow.exe","saintsrowiii.exe","saintsrowiv.exe",
    "maxpayne3.exe","sleepingdogs.exe","mafia3.exe","mafiadefinitiveedition.exe",
    "watchdogs.exe","watchdogs2.exe","watchdogslegion.exe",
    "farcry5.exe","farcry6.exe","farcry3.exe","farcry4.exe","farcrynewdawn.exe",

    # Roguelite / indie
    "riskofrain2.exe","ror2.exe","riskofrain.exe",
    "vampire-survivors.exe","vampiresurvivors.exe","vsurvivors.exe",
    "enterthegungeon.exe","gungeon.exe",
    "slaythespire.exe","stsone.exe",
    "deadcells.exe","dead-cells.exe",
    "tboi.exe","isaac-ng.exe","tbi.exe","isaacrebirth.exe","isaacrepentance.exe",
    "noita.exe","streetsofrage4.exe","cult of the lamb.exe","cultofthelamb.exe",
    "balatro.exe","stardewmodding.exe",
    "deeprockgalactic_survivor.exe","drgsurvivor.exe",
    "brotato.exe","backpackbattles.exe","bplunch.exe","peglin.exe",

    # MMO + live service
    "throneandliberty-win64-shipping.exe",
    "blackdesert.exe","blackdesert64.exe","bdlauncher.exe",
    "albiononline.exe","albion-online.exe","albion-online-launcher.exe",
    "ddolauncher.exe","dndlauncher.exe","ddo.exe","dnd.exe",
    "lotrolauncher.exe","lotroclient.exe","lotroclient64.exe",
    "wakfu-launcher.exe","wakfu.exe","dofus.exe",
    "newworld.exe","newworld_be.exe",
    "swtor.exe","swtorlauncher.exe",
    "tibialauncher.exe","tibia.exe",
    "runescape.exe","rs2client.exe","jagexlauncher.exe","osrsclient.exe","osclient.exe","runelite.exe",
    "everquest.exe","eqgame.exe","everquest2.exe","ffxi.exe",
    "blueprotocol.exe","throneandliberty.exe",

    # Racing sims (2024-2025)
    "lemansultimate.exe","lmu.exe","leemansultimate.exe",
    "rennsport.exe","rennsport-win64-shipping.exe",
    "ewrc.exe","earswrc.exe","wrc.exe","wrcgenerations.exe",
    "carx2.exe","carx-drift-racing-online2.exe",
    "drivelife.exe","testdrive_unlimited.exe","testdriveunlimited.exe","tdusc.exe",
    "rocketleaguesideswipe.exe","racing-master.exe",

    # Sports
    "nhl24.exe","nhl25.exe","nhl26.exe","nhl23.exe",
    "topspin2k25.exe","topspin.exe","ea sports tennis.exe",
    "thps12.exe","thpsremaster.exe","tony hawk's pro skater 1+2.exe",
    "ea sports pga tour.exe","golf with friends.exe","gwf.exe",

    # Strategy + sims
    "victoria3.exe","vic3.exe","oldworld.exe",
    "warhammerage of sigmar realmsofruin.exe","aosrealmsofruin.exe",
    "homm3hota.exe","heroes3.exe","mightandmagic.exe","kingsbounty2.exe","kbcrossworlds.exe",
    "wargame.exe","warno.exe","steeldivision2.exe","sd2.exe",
    "regimental.exe","regimentsgame.exe",
    "battletech.exe","mechwarrior5.exe","mw5mercs.exe","mwo.exe","mechwarrioronline.exe",
    "noproblems.exe","cities-skylines.exe","tropico6.exe","tropico5.exe",
    "kerbalspaceprogram.exe","ksp.exe","ksp2.exe","ksp_x64.exe",
    "factorio.exe","dwarffortress.exe","df.exe","caves of qud.exe","cogmind.exe",

    # Survival / sandbox
    "pacificdrive.exe","pacificdrive-win64-shipping.exe",
    "icarus.exe","icarus-win64-shipping.exe","prologue.exe",
    "thelongdark.exe","tld.exe","scum.exe","scumgame.exe",
    "stranded_deep.exe","strandeddeep.exe","themistery.exe",
    "raft.exe","craftopia.exe","valheim.exe",
    "wuthering-waves-win64-shipping.exe","towerofsavior.exe",
    "icarusepg.exe","fardescent.exe","aska.exe","grounded2.exe",
    "thelaststand.exe","tarisland.exe","tarisland-win64-shipping.exe",

    # Horror
    "outlast.exe","outlast2.exe","theoutlasttrials.exe","outlasttrials.exe","outlast-trials-win64-shipping.exe",
    "amnesia.exe","amnesia rebirth.exe","amnesiathebunker.exe","amnesia_thedarkdescent.exe",
    "thecallisto.exe","callisto.exe","thecallistoprotocol.exe",
    "deadspace.exe","deadspace2.exe","deadspace3.exe","deadspaceremake.exe","deadspace-remake.exe",
    "songsofconquest.exe","songsofconquest-win64-shipping.exe",
    "thefirstdescendant.exe","tfd.exe","m1-win64-shipping.exe",

    # Fighting / brawler
    "killerinstinct.exe","ki.exe","skullgirls.exe","under_night.exe","unico_lation.exe",
    "samurai.exe","samuraishodown.exe","kof15.exe","kingoffighters15.exe",
    "rivalsofaether.exe","rivalsofaether2.exe","ssbu.exe","melty.exe","meltyblood.exe",
    "punchout.exe","fight-night.exe","fightnight.exe",

    # Platformers / 3D adventure
    "ahatintime.exe","a hat in time.exe",
    "sonic.exe","sonicfrontiers.exe","sonicsuperstars.exe","sonicgenerations.exe","shadowgenerations.exe",
    "rayman.exe","raymanlegends.exe","raymanorigins.exe","ratchet&clank.exe","ratchetandclank.exe",
    "crashbandicoot.exe","crashbash.exe","crash4itsabouttime.exe","ctr.exe","crashteamracing.exe",
    "spongebob.exe","spongebobsquarepants.exe","spyroreignitedtrilogy.exe","spyro.exe",
    "marioandsonic.exe","mariomaker.exe","mariokart.exe","supermario64.exe",

    # Open world / RPG
    "elexii.exe","elex.exe","elex2.exe","gothic.exe","risen.exe",
    "kingdomsofamalur.exe","amalurreckoning.exe",
    "thelegendofzelda-totk.exe","totk.exe","botw.exe","zelda.exe",
    "ascendingtothewest.exe","seaofthieves.exe","sotgame.exe","sot.exe",
    "wolong.exe","wolongfd.exe","niohcomplete.exe","nioh2.exe","nioh.exe",
    "ghostsofyotei.exe","ghostofyotei.exe",
    "pentiment.exe","graveyardkeeper.exe","disco-elysium.exe","discoelysium.exe","de.exe",

    # Misc / popular streamers' picks
    "muckitup.exe","muck.exe","amongus.exe","goatsimulator.exe","goatsimulator3.exe",
    "schedule i.exe","schedule_i-win64-shipping.exe","scheduleone.exe",
    "drugdealersimulator.exe","drugdealer.exe",
    "weneedtoeat.exe","contentwarning2.exe",
    "bopl_battle.exe","ultimatechicken.exe","movingout.exe","movingout2.exe",
    "ultrakill.exe","dusk.exe","amid_evil.exe","amidevil.exe","cultic.exe",
    "anothercrabstreasure.exe","another_crab.exe",
    "diablohellfire.exe","torchlight.exe","torchlight2.exe","torchlightinfinite.exe",
    "guildwars.exe","gw.exe","gw1.exe","aionx64.exe","aion.exe",
}

_GAME_HINTS = ["-win64-shipping","-win32-shipping","game","dx11","dx12","vulkan","client-win","unreal"]

# Streaming / content-creation companions — these are prioritized when a game
# is running so streamers aren't disadvantaged. They are NOT treated as games
# on their own (they never trigger gaming-mode activation).
STREAMING_EXES = {
    # OBS family
    "obs64.exe","obs32.exe","obs.exe","obs-browser-page.exe",
    # Streamlabs
    "streamlabs desktop.exe","streamlabs obs.exe","slobs.exe",
    "streamlabs.exe","streamlabs desktop helper.exe",
    # XSplit
    "xsplit.broadcaster.exe","xsplit.core.exe","xsplit.gamecaster.exe",
    # Twitch Studio
    "twitchstudio.exe","twitch studio.exe",
    # NVIDIA/AMD capture
    "nvidiashare.exe","nvidia share.exe","relive.exe","amd relive.exe",
    # Other streaming
    "restream chat.exe","restream.io.exe","restreamstudio.exe",
    "lightstream.exe","prism live studio.exe","prismlivestudio.exe",
    "melonstream.exe","cast.exe",
    # Chat / overlay tools streamers use
    "streamdeck.exe","stream deck.exe","streamelements.exe",
    "streamlabels.exe","chatterino.exe","chatty.exe",
    # Recording
    "nvenc.exe","shadowplay.exe","action.exe","bandicam.exe",
    "fraps.exe","dxtory.exe",
    # Voice stack frequently paired with streaming
    "voicemod.exe","voicemoddesktop.exe",
    "mentor.exe","mic+.exe","vb-cable.exe","voicemeeter.exe",
    # Discord (streamers often stream TO Discord or need low Discord latency)
    "discord.exe","discordcanary.exe","discordptb.exe",
}

_IGNORE_PROCS = {
    "explorer.exe","svchost.exe","system","idle","registry",
    "smss.exe","csrss.exe","wininit.exe","services.exe",
    "lsass.exe","dwm.exe","taskhostw.exe","runtimebroker.exe",
    "powershell.exe","cmd.exe","conhost.exe","python.exe",
    "pythonw.exe","node.exe","code.exe","chrome.exe",
    "firefox.exe","msedge.exe","opera.exe","brave.exe",
    "spotify.exe","steam.exe","steamwebhelper.exe",
    "epicgameslauncher.exe","unrealcefsubprocess.exe",
    "originwebhelperservice.exe","eadesktop.exe",
    "battlenet.exe","agent.exe",
    "nvcontainer.exe",
    "searchhost.exe","startmenuexperiencehost.exe",
    "textinputhost.exe","applicationframehost.exe",
    "securityhealthservice.exe","securityhealthsystray.exe",
    "widgets.exe","phoneexperiencehost.exe",
    "gamebarpresencewriter.exe","gamebarftserver.exe",
    "msmpeng.exe",*OWN_PROCESS_NAMES,"app.py","flask.exe",
    "onedrive.exe","teams.exe","outlook.exe",
    "wallpaperengine32.exe","wallpaper32.exe",
}


# ═══════════════════════════════════════════════════════════════
# Engine State
# ═══════════════════════════════════════════════════════════════
_engine = {
    "monitoring": False,
    "thread": None,
    "active_game": None,
    "active_exe": None,
    "active_pid": None,
    "gaming_mode": False,
    "changes": [],
    "streaming_exes": [],
    # v3 — captured at game-start so crash_recovery knows what OC was
    # active when (if) the game crashes.
    "active_oc_core": 0,
    "active_oc_mem":  0,
    # What process_tuning.tune() changed on the tracked process, for release().
    "proc_prior": None,
    # Every process of the game's exe, keyed by pid: {"prior", "created",
    # "result" (the last tune()), "fixes" (times it was re-applied), "gave_up"}.
    "game_image": "",
    "game_procs": {},
    # What is in effect, as last read back: [{"key", "ok", "text"}].  `changes`
    # is the same as plain lines, for older callers.
    "checks": [],
    "verified_at": 0.0,
    "power_applied": [],            # [(scheme, setting, value)] set for this session, for the re-checks
    "streamer_prior": {},           # pid -> priority class a streaming app had before it was raised
    "exit_ticks": 0,                # ticks since the last process of the game went away
    "tick": 0,
    # Background programs in efficiency mode for the session (see _eco_sweep).
    "eco_on": False,
    "eco_session": None,            # the Windows session the game runs in
    "eco_seen": {},                 # pid -> exe of every process already looked at
    "eco_procs": {},                # pid -> {"exe", "created", "before"}: what to put back
    "eco_failed": 0,                # processes that did not read back as in efficiency mode
}
# One session start, tick or restore at a time.  Two monitor threads used to
# overlap for a moment when the monitor was restarted (adding a custom game
# does that): the new one captured its "normal" baseline while the old one was
# half-way through applying the gaming values — and restored those for good.
_session_lock = threading.RLock()
_TICK_S = 3
_EXIT_GRACE_TICKS = 4               # ~12 s: a game that restarts itself is back by then
_SYSTEM_CHECK_TICKS = 10            # system-wide settings are re-read every ~30 s
_REAPPLY_LIMIT = 5                  # per process, within _REAPPLY_WINDOW_S; past that the game wins
_REAPPLY_WINDOW_S = 300


def note_active_oc(core: int, mem: int) -> None:
    """beta.5 — Adaptive Tuning calls this whenever it applies a NEW offset so
    the crash-recovery snapshot (_engine['active_oc_*']) always reflects what is
    ACTUALLY on the card, not the value captured once at game-start.

    Without it, AT's startup-guard fast-forward (and any subsequent step-up)
    changed the real clocks while _engine stayed frozen at baseline — so a
    climbed offset that TDR-crashed the game got mis-attributed to baseline,
    never blacklisted, and re-applied every launch (an indefinite crash loop).
    Only meaningful while a game is active; a no-op otherwise so stray AT/manual
    applies outside gaming mode don't leave a phantom active OC behind."""
    if _engine.get("gaming_mode"):
        _engine["active_oc_core"] = int(core)
        _engine["active_oc_mem"]  = int(mem)


# ═══════════════════════════════════════════════════════════════
# Baseline — capture & restore "normal" system state
# ═══════════════════════════════════════════════════════════════
def _capture_baseline() -> dict:
    """Capture current system settings as the 'normal' baseline."""
    baseline = {}

    # Win32PrioritySeparation
    r = run_cmd(["reg","query",r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl","/v","Win32PrioritySeparation"])
    if r["ok"] and "0x" in r["out"]:
        baseline["priority_sep"] = r["out"].split("0x")[-1].strip().split()[0]

    # SystemResponsiveness
    r = run_cmd(["reg","query",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile","/v","SystemResponsiveness"])
    if r["ok"] and "0x" in r["out"]:
        baseline["sys_responsiveness"] = r["out"].split("0x")[-1].strip().split()[0]

    # NetworkThrottlingIndex
    r = run_cmd(["reg","query",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile","/v","NetworkThrottlingIndex"])
    if r["ok"] and "0x" in r["out"]:
        baseline["net_throttle"] = r["out"].split("0x")[-1].strip().split()[0]

    # CPU idle
    baseline["cpu_idle_disabled"] = _cpu_idle_disabled()
    baseline["idle_probe"] = "qh"        # this baseline's idle state is a real reading

    return baseline


def _cpu_idle_disabled() -> bool:
    """Is processor idle switched off (on AC) in the active power plan?

    IdleDisable is a hidden setting, and `powercfg /query` does not list
    hidden settings at all — so this used to come back "not disabled" whatever
    the truth was, and every game exit then switched idle back ON for people
    who had turned it off on purpose."""
    cur = reg_originals.query_power("SCHEME_CURRENT", "SUB_PROCESSOR", "IdleDisable")
    return bool(cur and cur[0] == 1)


def _save_baseline(baseline: dict):
    atomic_write_json(BASELINE_FILE, baseline)


def _load_baseline() -> dict:
    if os.path.exists(BASELINE_FILE):
        try:
            with open(BASELINE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _check(key: str, ok, text: str) -> None:
    """Record what one item of game mode really is now (ok: True, False, or
    None when it cannot be told), replacing an earlier entry for it."""
    _engine["checks"] = [c for c in _engine.get("checks", []) if c["key"] != key] + \
        [{"key": key, "ok": ok, "text": text}]
    _engine["changes"] = [c["text"] for c in _engine["checks"]]
    _engine["verified_at"] = time.time()


def _set_gaming_flag():
    """Write a flag file indicating gaming tweaks are active."""
    try:
        with open(GAMING_FLAG_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _clear_gaming_flag():
    try:
        if os.path.exists(GAMING_FLAG_FILE):
            os.remove(GAMING_FLAG_FILE)
    except Exception:
        pass


def _is_gaming_flag_set() -> bool:
    return os.path.exists(GAMING_FLAG_FILE)


# ═══════════════════════════════════════════════════════════════
# Apply / Revert
# ═══════════════════════════════════════════════════════════════
# ── the game's own processes ─────────────────────────────────────────
def _tune_one(pid: int, rec: dict, all_cores: bool) -> None:
    """tune() one process and keep what release() will need: the state it
    had before game mode first touched it, whatever later re-applies saw."""
    r = process_tuning.tune(pid, all_cores=all_cores)
    if not r.get("ok"):
        # The handle was refused (anti-cheat, usually).  The old PowerShell
        # route, for what it is worth — counted only if it reads back as done.
        ps = run_ps(f"try {{ (Get-Process -Id {int(pid)}).PriorityClass='High'; 'OK' }} catch {{ 'FAIL' }}", timeout=5)
        if ps.get("ok") and "OK" in (ps.get("out") or ""):
            st = process_tuning.read_state(pid)
            if st.get("ok"):
                r["priority"] = st.get("priority") == process_tuning.HIGH_PRIORITY_CLASS
            else:
                r["priority_unchecked"] = True
    if r.get("ok"):
        if rec["prior"] is None:
            rec["prior"] = dict(r.get("prior") or {})
        else:
            for k, v in (r.get("prior") or {}).items():
                rec["prior"].setdefault(k, v)
    rec["result"] = r
    if pid == _engine.get("active_pid"):
        _engine["proc_prior"] = rec["prior"]


def _drift(pid: int, rec: dict, all_cores: bool) -> list:
    """What of this process's tuning is no longer in effect (read back now)."""
    st = process_tuning.read_state(pid)
    if not st.get("ok"):
        return []
    r, out = rec["result"], []
    if r.get("priority") and st.get("priority") != process_tuning.HIGH_PRIORITY_CLASS:
        out.append("priority")
    if all_cores and (r.get("affinity") or {}).get("ok") and st.get("system") and st.get("affinity") != st.get("system"):
        out.append("CPU affinity")
    if r.get("throttling") and st.get("throttle_exempt") is False:
        out.append("power throttling")
    return out


def _tune_game_processes(pids: list, recheck: bool = False) -> None:
    """High priority, every CPU core, no power throttling — for every process
    of the game's exe (some games run more than one, and some restart
    themselves), each read back afterwards.  Re-checked at every monitor
    tick: when the game, its launcher or its anti-cheat changes something
    back, it is applied again, up to _REAPPLY_LIMIT times in
    _REAPPLY_WINDOW_S per process — past that the game is left to have its
    way, and the status says so.  (It used to be applied once at detection
    and reported as done from then on, whatever the process was set to.)"""
    all_cores = bool(_load_gp_settings().get("all_cores", True))
    procs = _engine["game_procs"]
    now = time.time()
    exe = _engine.get("active_exe") or "the game"
    for pid in pids:
        rec = procs.get(pid)
        if rec is None:
            rec = procs[pid] = {"prior": None, "created": process_tuning.identity(pid).get("created"),
                                "result": {}, "fixes": [], "fixed": {}, "gave_up": False}
            _tune_one(pid, rec, all_cores)
            if len(procs) > 1:
                log.info(f"  {exe}: tuned another process of the game (PID {pid})")
            continue
        if rec["gave_up"]:
            continue
        if not rec["result"].get("ok"):
            if recheck:             # refused before: some anti-cheat allows it once the game is running
                _tune_one(pid, rec, all_cores)
            continue
        drift = _drift(pid, rec, all_cores)
        if not drift:
            continue
        rec["fixes"] = [t for t in rec["fixes"] if now - t < _REAPPLY_WINDOW_S]
        if len(rec["fixes"]) >= _REAPPLY_LIMIT:
            rec["gave_up"] = True
            rec["gave_up_on"] = drift
            log.warning(f"  {exe} (PID {pid}) keeps changing its own {', '.join(drift)} — "
                        f"no longer forced (re-applied {_REAPPLY_LIMIT}x in {_REAPPLY_WINDOW_S // 60} min)")
            continue
        rec["fixes"].append(now)
        for item in drift:
            rec["fixed"][item] = rec["fixed"].get(item, 0) + 1
        log.info(f"  {exe} (PID {pid}): its {', '.join(drift)} had been changed back — re-applied")
        _tune_one(pid, rec, all_cores)
    for gone in [p for p in procs if p not in pids]:
        procs.pop(gone)
    _report_processes(all_cores)


def _report_processes(all_cores: bool) -> None:
    recs = list(_engine["game_procs"].values())
    if not recs:
        return
    n = len(recs)
    several = f" ({n} processes)" if n > 1 else ""
    opened = [r for r in recs if r["result"].get("ok")]

    def again(item):
        times = sum(r["fixed"].get(item, 0) for r in recs)
        return f" — re-applied {times}x after the game changed it back" if times else ""

    def given_up(item):
        return any(r["gave_up"] and item in r.get("gave_up_on", []) for r in recs)

    # priority
    if given_up("priority"):
        _check("proc:priority", False, "Process priority: the game keeps setting its own — no longer forced")
    elif all(r["result"].get("priority") for r in recs):
        _check("proc:priority", True, "Process priority -> High" + several + again("priority"))
    elif any(r["result"].get("priority_unchecked") for r in recs) and not opened:
        _check("proc:priority", None, "Process priority -> High (set through PowerShell; Windows does not let it be checked)")
    elif not opened:
        _check("proc:priority", False, "Process priority: unchanged (the game's process is protected)")
    else:
        done = sum(1 for r in recs if r["result"].get("priority"))
        if done == 0:
            _check("proc:priority", False, "Process priority: Windows accepted High but the game is still not at it "
                   "(its anti-cheat or the game itself holds it back)")
        else:
            _check("proc:priority", False, f"Process priority -> High on {done} of {n} processes (the others refused it)")

    # every core
    if all_cores:
        main = _engine["game_procs"].get(_engine.get("active_pid")) or recs[0]
        aff = main["result"].get("affinity") or {}
        if given_up("CPU affinity"):
            _check("proc:affinity", False, "CPU affinity: the game keeps restricting itself — no longer forced")
        elif not opened:
            _check("proc:affinity", False, "CPU affinity: unchanged (the game's process is protected)")
        elif all((r["result"].get("affinity") or {}).get("ok") for r in opened):
            _check("proc:affinity", True, f"CPU affinity -> all {process_tuning.describe_cores()}"
                   + (f" (was limited to {aff['was']})" if aff.get("changed") else "") + several + again("CPU affinity"))
        else:
            _check("proc:affinity", False, "CPU affinity: could not be widened to every core")
    else:
        _engine["checks"] = [c for c in _engine["checks"] if c["key"] != "proc:affinity"]

    # power throttling
    if opened:
        if given_up("power throttling"):
            _check("proc:throttle", False, "Power throttling: the game keeps opting back in — no longer forced")
        elif all(r["result"].get("throttling") for r in opened):
            _check("proc:throttle", True, "Power throttling -> off for the game" + again("power throttling"))
        else:
            _check("proc:throttle", False, "Power throttling: could not be switched off for the game")


# ── system-wide values ───────────────────────────────────────────────
_REG_TWEAKS = (
    # key, value, data, what it means
    (r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", 40,
     "Win32PrioritySeparation -> 0x28 (Short/Fixed)"),
    # beta.6: was 0.  Setting 0 dedicates 100% of the multimedia class to
    # foreground threads and STARVES Audiosrv (Discord / in-game voice
    # crackle under load).  The rest of the app (optimizer, network_tweaks)
    # standardized on Microsoft's gaming value 10, and auto_recover actively
    # reverts any 0 it finds — so a per-game 0 here just got undone.  Aligned.
    (r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "SystemResponsiveness", 10,
     "SystemResponsiveness -> 10"),
    (r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "NetworkThrottlingIndex",
     4294967295, "Network throttling -> disabled"),
)


def _reads(key: str, name: str, data: int) -> bool:
    cur = reg_originals.read_value(key, name) or {}
    return cur.get("type") == "REG_DWORD" and cur.get("data") == data


def _apply_registry_tweaks(recheck: bool = False) -> None:
    """Each value's previous state is recorded (scope "gamemode") before it
    is changed, so the exit puts back exactly that.  It used to be put back
    from a separately captured "baseline" — captured, once, while another
    monitor thread had the gaming values in place, and from then on restored
    as "normal" after every game.  On a re-check, a value that is no longer on
    this session's record (the optimizer or the user has set it since) is
    theirs, and is left alone."""
    for key, name, data, text in _REG_TWEAKS:
        what = text.split(" ->")[0]
        if _reads(key, name, data):
            if not recheck:
                _check(f"reg:{name}", True, text)
            continue
        if recheck and not reg_originals.has_original(key, name, scope=_SCOPE):
            _check(f"reg:{name}", None, f"{what}: changed since by another setting — left as it is")
            continue
        reg_originals.remember(key, name, str(data), "REG_DWORD", scope=_SCOPE)
        run_cmd(["reg", "add", key, "/v", name, "/t", "REG_DWORD", "/d", str(data), "/f"])
        ok = _reads(key, name, data)
        if recheck and ok:
            log.info(f"  {name} had been changed back — re-applied")
            text += " — re-applied after it was changed back"
        _check(f"reg:{name}", ok, text if ok else f"{what}: could not be set")


# ── power plan ───────────────────────────────────────────────────────
_HEX8 = re.compile(r"0x[0-9a-fA-F]{8}")


def _power_limit(scheme: str, setting: str):
    """The largest value Windows accepts for this processor setting on this
    PC, or None if that cannot be read.  (/qh lists, for a range setting:
    minimum, maximum, increment, then the AC and DC values.)"""
    r = run_cmd(["powercfg", "/qh", scheme, _PROC_SUB, setting])
    found = _HEX8.findall(r.get("out") or "") if r.get("ok") else []
    return int(found[-4], 16) if len(found) >= 5 else None


def _apply_power_tweaks(recheck: bool = False) -> None:
    """The power-plan half of game mode: no parked cores, no CPU idle states.

    Runs AFTER apply_target(), on whichever plan is active by then, and
    stays on that plan for the re-checks.  Each previous value is recorded
    per plan and put back by restore_normal_mode(), so nothing can land on
    the wrong plan.  Every setting is read back after it is set — a setting
    Windows refuses (on one PC an old tweak had capped "core parking min
    cores" at 0) is reported as not done instead of as done."""
    if not recheck:
        # Marks the session as one whose plan changes are all on record.
        reg_originals.note("gamemode_plan_recorded")
        _engine["power_scheme"] = _active_power_guid().lower()
    scheme = _engine.get("power_scheme") or ""
    if not scheme:
        _check("power:parking", None, "Power plan: could not be read, so it was left as it is")
        return
    hybrid = bool(process_tuning.cpu_layout().get("hybrid"))
    cores = {_CORE_PARKING_MIN[0]: "E-cores" if hybrid else "cores", _CORE_PARKING_MIN[1]: "P-cores"}
    outcome, changed = {}, False
    limits = _engine.setdefault("power_limits", {})
    for setting in (_CORE_PARKING_MIN if hybrid else _CORE_PARKING_MIN[:1]):
        cur = reg_originals.query_power(scheme, _PROC_SUB, setting)
        if setting not in limits or not recheck:
            limits[setting] = _power_limit(scheme, setting)
        limit = limits[setting]
        if limit is not None and limit < 100:
            outcome[setting] = "capped"
        elif cur and cur[0] >= 100:
            outcome[setting] = "on"
        elif recheck and not reg_originals.has_power(_PROC_SUB, setting, scope=_SCOPE, scheme=scheme):
            outcome[setting] = "not ours"
        elif not reg_originals.remember_power(_PROC_SUB, setting, scope=_SCOPE):
            outcome[setting] = "unreadable"
        else:
            run_cmd(["powercfg", "/setacvalueindex", scheme, _PROC_SUB, setting, "100"])
            now = reg_originals.query_power(scheme, _PROC_SUB, setting)
            outcome[setting] = "on" if now and now[0] == 100 else "refused"
            changed = changed or outcome[setting] == "on"
    good = [s for s, o in outcome.items() if o == "on"]
    if len(good) == len(outcome):
        if not (recheck and not changed):
            _check("power:parking", True, "Core parking -> off (every core kept awake)"
                   + (" — re-applied after it was changed back" if recheck and changed else ""))
    else:
        bad = [s for s in outcome if s not in good]
        why = []
        if any(outcome[s] == "capped" for s in bad):
            why.append("an old tweak capped Windows' limit for that setting at 0 (Reset All on the Optimizer "
                       "page repairs it)")
        if any(outcome[s] in ("refused", "unreadable") for s in bad):
            why.append("Windows did not accept the change")
        if any(outcome[s] == "not ours" for s in bad):
            why.append("it has been changed since by another setting")
        head = (f"Core parking -> off for the {' and '.join(cores[s] for s in good)}; " if good else "Core parking: ")
        _check("power:parking", False, head + f"the {' and '.join(cores[s] for s in bad)} could not be changed: "
               + "; ".join(why))

    # CPU idle disabled (max clocks) — likewise only when it can be put back.
    cur = reg_originals.query_power(scheme, "SUB_PROCESSOR", "IdleDisable")
    if cur and cur[0] == 1:
        if not recheck:
            _check("power:idle", True, "CPU idle -> disabled")
    elif recheck and not reg_originals.has_power("SUB_PROCESSOR", "IdleDisable", scope=_SCOPE, scheme=scheme):
        _check("power:idle", None, "CPU idle: changed since by another setting — left as it is")
    elif reg_originals.remember_power("SUB_PROCESSOR", "IdleDisable", scope=_SCOPE):
        run_cmd(["powercfg", "/setacvalueindex", scheme, "SUB_PROCESSOR", "IdleDisable", "1"])
        now = reg_originals.query_power(scheme, "SUB_PROCESSOR", "IdleDisable")
        ok = bool(now and now[0] == 1)
        changed = changed or ok
        _check("power:idle", ok, ("CPU idle -> disabled" + (" — re-applied after it was changed back" if recheck else ""))
               if ok else "CPU idle: Windows did not accept the change")
    else:
        _check("power:idle", False, "CPU idle: its current setting could not be read, so it was not changed")
    if changed or not recheck:
        run_cmd(["powercfg", "/setactive", scheme])


# ── network priority and streaming apps ──────────────────────────────
def _raise_streamers(streamers: list) -> dict:
    """AboveNormal for each streaming app's processes (not High — the game
    gets High).  Returns {exe: (raised, total)}."""
    counts = {}
    for s in streamers:
        done, before = process_tuning.set_priority(s["pid"], process_tuning.ABOVE_NORMAL_PRIORITY_CLASS)
        if done and before and before != process_tuning.ABOVE_NORMAL_PRIORITY_CLASS:
            _engine["streamer_prior"][s["pid"]] = before
        raised, total = counts.get(s["exe"], (0, 0))
        counts[s["exe"]] = (raised + bool(done), total + 1)
    return counts


def _apply_network_priority(exe_name: str, streamers: list) -> None:
    """DSCP 46 for the game's traffic and 34 for the streaming apps' — in ONE
    PowerShell call, each policy looked up afterwards.  This used to be two
    calls per streaming process: with Discord's eight processes, half a minute
    passed between "game detected" and the rest of game mode."""
    counts = _raise_streamers(streamers)
    rules = []
    if is_safe_ps_exe_name(exe_name):
        rules.append(("qos:game", f"{APP_NAME}_Game_{exe_name.replace('.exe', '').replace(' ', '_')}", exe_name, 46))
    else:
        log.warning(f"  skipped DSCP QoS for unsafe exe name: {exe_name!r}")
    for s_exe in sorted(counts):
        if is_safe_ps_exe_name(s_exe):
            rules.append((f"qos:{s_exe}", f"{APP_NAME}_Stream_{s_exe.replace('.exe', '').replace(' ', '_')}", s_exe, 34))
        else:
            log.warning(f"  skipped stream QoS for unsafe exe name: {s_exe!r}")
    made = set()
    if rules:
        script = "$ErrorActionPreference = 'SilentlyContinue'\n" + "\n".join(
            f"Remove-NetQosPolicy -Name '{name}' -Confirm:$false\n"
            f"New-NetQosPolicy -Name '{name}' -AppPathNameMatchCondition '{exe}' -DSCPAction {dscp} "
            f"-NetworkProfile All -Confirm:$false | Out-Null\n"
            f"if (Get-NetQosPolicy -Name '{name}') {{ 'OK {name}' }}"
            for _, name, exe, dscp in rules)
        r = run_ps(script, timeout=90)
        made = {ln.strip()[3:] for ln in (r.get("out") or "").splitlines() if ln.strip().startswith("OK ")}
    for key, name, exe, _ in rules:
        if key == "qos:game":
            _check(key, name in made, f"DSCP 46 for {exe}" if name in made
                   else f"Network priority (DSCP 46) for {exe}: the rule could not be created")
        else:
            raised, total = counts.get(exe, (0, 0))
            several = f" ({total} processes)" if total > 1 else ""
            if name in made and raised == total:
                _check(key, True, f"Stream priority: {exe}{several}")
            else:
                parts = [] if name in made else ["network rule could not be created"]
                if raised < total:
                    parts.append(f"priority raised for {raised} of {total} processes")
                _check(key, False, f"Stream priority: {exe}{several}: " + "; ".join(parts))
    _engine["streaming_exes"] = sorted(counts)
    if counts:
        log.info("  Prioritized streaming app(s): " + ", ".join(
            f"{e} ({counts[e][1]} processes)" if counts[e][1] > 1 else e for e in sorted(counts)))


# ── background programs on the efficiency cores ──────────────────────
# Never put in efficiency mode: what draws the screen, makes the sound or
# carries the input, the shell, anti-cheat, overlays and recorders — and
# Vispora, whose gamepad mapper polls at 4 kHz.
_ECO_KEEP = frozenset({
    "system", "registry", "idle", "memory compression", "secure system",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
    "dwm.exe", "fontdrvhost.exe", "audiodg.exe", "svchost.exe", "explorer.exe", "sihost.exe",
    "ctfmon.exe", "textinputhost.exe", "logonui.exe", "conhost.exe",
    "steam.exe",                                    # Steam Input runs in it
    "ds4windows.exe", "dsx.exe", "x360ce.exe", "joytokey.exe", "antimicrox.exe",
    "gamebar.exe", "gamebarftserver.exe", "nvcontainer.exe", "nvidia share.exe", "nvidia overlay.exe",
    "rtss.exe", "msiafterburner.exe", "steelseriessonar.exe",
    *STREAMING_EXES, *OWN_PROCESS_NAMES, "app.py",
})
_ECO_KEEP_PREFIXES = (
    "easyanticheat", "eaanticheat", "beservice", "battleye", "faceit", "vgc", "vgtray",     # anti-cheat
    "razer", "lghub", "logi", "icue", "corsair", "steelseries", "wooting",          # mouse / keyboard software
    "nahimic",
)
_ECO_FILE = os.path.join(PROFILES_DIR, "eco_restore.json")


def _eco_keep(exe: str) -> bool:
    return exe in _ECO_KEEP or exe.startswith(_ECO_KEEP_PREFIXES)


def _save_eco() -> None:
    """What is in efficiency mode because of us, on disk too: after a crash
    the next start puts it back (see check_and_restore_on_boot)."""
    recs = _engine.get("eco_procs") or {}
    try:
        if recs:
            atomic_write_json(_ECO_FILE, {str(pid): r for pid, r in recs.items()})
        elif os.path.exists(_ECO_FILE):
            os.remove(_ECO_FILE)
    except Exception as e:
        log.debug(f"could not save the efficiency-mode records: {e}")


def _eco_sweep() -> None:
    """Every other program in the user's session into efficiency mode, so a
    hybrid CPU's scheduler puts them on the E-cores and leaves the P-cores to
    the game (process_tuning.to_efficiency).  At session start, then at every
    tick for what has been opened since: each process is looked at once, and
    one that later changes its own setting is not fought.  Never the game,
    nor anything it or Vispora started."""
    procs = process_tuning.processes()
    if procs is None:
        return
    seen, eco = _engine["eco_seen"], _engine["eco_procs"]
    game_exe = (_engine.get("active_exe") or "").lower()
    children: dict = {}
    for p in procs:
        children.setdefault(p["ppid"], []).append(p["pid"])
    tree, todo = set(), list(_engine.get("game_procs") or {}) + [_engine.get("active_pid"), os.getpid()]
    while todo:
        pid = todo.pop()
        if pid is not None and pid not in tree:
            tree.add(pid)
            todo.extend(children.get(pid, []))
    changed, moved = False, []
    # A process of the game that was a background program when it was put in
    # efficiency mode (a second game, now the tracked one) comes back out.
    for pid in [p for p, r in eco.items() if p in tree or r["exe"] == game_exe]:
        if process_tuning.from_efficiency(pid, eco[pid]["created"], eco[pid]["before"]):
            eco.pop(pid)
            changed = True
    session = _engine.get("eco_session")
    for p in procs:
        pid, exe = p["pid"], p["exe"].lower()
        if seen.get(pid) == exe:
            continue
        seen[pid] = exe
        if pid in (0, 4) or pid in tree or exe == game_exe or _eco_keep(exe):
            continue
        if session is not None and process_tuning.session_of(pid) != session:
            continue                    # services, and other users' programs
        r = process_tuning.to_efficiency(pid)
        if r["done"]:
            eco[pid] = {"exe": exe, "created": r["created"], "before": r["before"]}
            moved.append(exe)
            changed = True
        elif not r["skip"]:
            _engine["eco_failed"] = _engine.get("eco_failed", 0) + 1
    live = {p["pid"] for p in procs}
    for pid in [p for p in seen if p not in live]:
        seen.pop(pid)
    for pid in [p for p in eco if p not in live]:
        eco.pop(pid)                    # exited: nothing left to put back
        changed = True
    if changed:
        _save_eco()
    if moved:
        log.info(f"  Efficiency mode for {len(moved)} background process(es): {', '.join(sorted(set(moved)))}")
    _report_eco()


def _report_eco() -> None:
    eco = _engine.get("eco_procs") or {}
    programs = len({r["exe"] for r in eco.values()})
    if eco:
        _check("eco", True, f"Background programs -> efficiency cores ({programs} program{'s' * (programs != 1)}, "
                            f"{len(eco)} process{'es' * (len(eco) != 1)})")
    elif _engine.get("eco_failed"):
        _check("eco", False, "Background programs: Windows did not keep them in efficiency mode")
    else:
        _check("eco", None, "Background programs: none to move to the efficiency cores")


def _eco_tick() -> None:
    if not _engine.get("eco_on"):
        return
    if not _load_gp_settings().get("background_on_ecores", True):
        _engine["eco_on"] = False                   # switched off mid-game: put back now
        _eco_restore()
        _engine["checks"] = [c for c in _engine["checks"] if c["key"] != "eco"]
        _engine["changes"] = [c["text"] for c in _engine["checks"]]
        return
    _eco_sweep()


def _eco_restore() -> None:
    """Every background process game mode put in efficiency mode back as it
    was — those still running, still the same process, and not changed by
    themselves since.  From the file when the session's records are gone
    (a crash)."""
    recs = dict(_engine.get("eco_procs") or {})
    if not recs and os.path.exists(_ECO_FILE):
        try:
            with open(_ECO_FILE, encoding="utf-8") as f:
                recs = {int(pid): r for pid, r in (json.load(f) or {}).items()}
        except Exception as e:
            log.debug(f"could not read the efficiency-mode records: {e}")
    back = 0
    for pid, r in recs.items():
        try:
            if r.get("before") is not None and process_tuning.alive(pid, r.get("created")) is not False:
                back += bool(process_tuning.from_efficiency(pid, r.get("created"), r["before"]))
        except Exception as e:
            log.debug(f"could not put process {pid} back out of efficiency mode: {e}")
    _engine.update(eco_on=False, eco_seen={}, eco_procs={}, eco_failed=0)
    _save_eco()
    if back:
        log.info(f"  Put {back} background process(es) back out of efficiency mode")


def _apply_session_extras(exe_name: str) -> None:
    """Vispora's own responsiveness and the optional background pauser."""
    # v3.4 — bump Vispora out of efficiency mode so the mapper /
    # HidHide retry / network monitor / etc. stay responsive while the
    # game runs at HIGH priority.  Without this, Vispora sits at
    # Below-Normal + EcoQoS on E-cores and the mapper's 4 kHz polling
    # drops to ~200 Hz.
    try:
        import sys
        # The function lives in app.py — we import it lazily so this
        # module doesn't depend on the Flask app at import time.
        main_app = sys.modules.get("app") or sys.modules.get("__main__")
        if main_app and hasattr(main_app, "_set_responsive_mode"):
            main_app._set_responsive_mode()
    except Exception as e:
        log.debug(f"could not set responsive mode: {e}")

    # Background pauser — freeze AI apps + browsers (opt-in)
    try:
        from core import background_pauser
        if background_pauser.get_settings().get("enabled"):
            r = background_pauser.pause_now(reason=f"game start: {exe_name}")
            if r.get("ok") and r.get("paused"):
                _check("bg-pause", True, f"Paused {len(r['paused'])} bg apps (~{r['total_freed_mb']} MB)")
                log.info(f"  Paused {len(r['paused'])} background apps, "
                         f"freed ~{r['total_freed_mb']} MB")
    except Exception as e:
        log.debug(f"background pauser hook failed: {e}")


# ── Per-game optimization target dispatch (beta.13) ──────────────────
# Holds what apply_target() changed for the CURRENTLY-tracked game, so
# restore_target() can reverse exactly that on exit.  The engine tracks
# one game at a time, so a single slot is sufficient.
_target_restore: dict = {}
_TARGET_RESTORE_FILE = os.path.join(PROFILES_DIR, "target_restore.json")


def _save_target_restore(restore: dict) -> None:
    """Keep what apply_target() changed on disk too.  After a crash or power
    cut mid-game the in-memory copy is gone — and with it the power plan to
    go back to, while Competitive Latency stayed switched on for good: the
    next launch found it "already on" and so never switched it off again."""
    try:
        if restore:
            atomic_write_json(_TARGET_RESTORE_FILE, restore)
        elif os.path.exists(_TARGET_RESTORE_FILE):
            os.remove(_TARGET_RESTORE_FILE)
    except OSError as e:
        log.debug(f"could not update {_TARGET_RESTORE_FILE}: {e}")


def _load_target_restore() -> dict:
    try:
        with open(_TARGET_RESTORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _active_power_guid() -> str:
    """GUID of the currently-active Windows power scheme, or '' if unknown."""
    r = run_cmd(["powercfg", "/getactivescheme"], timeout=6)
    if not r.get("ok"):
        return ""
    for part in (r.get("out") or "").split():
        if len(part) == 36 and part.count("-") == 4:
            return part
    return ""


def apply_target(exe: str) -> None:
    """Compose the perf posture for this game's optimization target
    (auto-classified / set in adaptive_tuning state).  Called at game-start,
    right before AT's own engage.  Reuses existing primitives and captures
    what it changes so restore_target() can reverse it on exit.

      max_fps     → set the FLOOR: the user's proven saved OC at 100% GPU
                    power + Ultimate power plan.  Adaptive Tuning (enabled +
                    visual for max_fps games) then climbs ABOVE this floor
                    for more frames, gated by stability + temp ceiling.
      low_latency → engage Competitive Latency for the session (timer,
                    flip-model, 1:1 mouse, DVR/throttle off).  GPU OC isn't
                    the lever for these CPU-bound titles, so it's untouched.
      auto/balanced → nothing; the normal mode-driven AT flow runs.
    """
    global _target_restore
    try:
        from core import adaptive_tuning
        target = (adaptive_tuning.get_game_state(exe) or {}).get("target", "auto")
    except Exception:
        target = "auto"
    if target in ("auto", "balanced"):
        _target_restore = {}
        _save_target_restore({})
        return
    restore = {"target": target, "exe": exe}
    try:
        if target == "max_fps":
            from core import gpu_overclock, optimizer
            restore["power_guid"] = _active_power_guid()
            prof = gpu_overclock.load_profile()
            has = bool(prof.get("exists"))
            restore["oc"] = {
                "core":  int(prof.get("core_offset_mhz", 0) or 0) if has else 0,
                "mem":   int(prof.get("mem_offset_mhz", 0) or 0) if has else 0,
                "power": int(prof.get("power_pct", 100) or 100) if has else 100,
            }
            _save_target_restore(restore)       # on disk before anything changes
            optimizer.apply_ultimate_power_plan()
            gpu_overclock.apply_oc(core_offset_mhz=restore["oc"]["core"],
                                   mem_offset_mhz=restore["oc"]["mem"],
                                   power_pct=100)
            log.info(f"  Max-FPS target: held OC core+{restore['oc']['core']}/"
                     f"mem+{restore['oc']['mem']} @ 100% power + Ultimate power plan")
        elif target == "low_latency":
            from core import competitive_latency
            already = bool(competitive_latency.status().get("enabled"))
            restore["we_enabled_latency"] = (not already)
            if not already:
                _save_target_restore(restore)   # on disk before anything changes
                competitive_latency.apply()
                log.info("  Low-Latency target: engaged Competitive Latency for this session")
            else:
                log.info("  Low-Latency target: Competitive Latency already on — leaving as-is")
    except Exception as e:
        log.warning(f"apply_target({target}) failed: {e}")
    _target_restore = restore


def restore_target() -> None:
    """Reverse whatever apply_target() applied for the just-exited game."""
    global _target_restore
    r, _target_restore = _target_restore, {}
    recovered = False
    if not r:
        r = _load_target_restore()      # the app has been restarted since that game started
        recovered = bool(r)
    _save_target_restore({})
    if not r:
        return
    target = r.get("target")
    try:
        if target == "max_fps":
            from core import gpu_overclock
            oc = r.get("oc") or {}
            # Re-apply the saved profile (resets GPU power_pct to the user's
            # normal value) then restore the prior power plan.
            # Not after a restart: the card is back at its own defaults by
            # then, and whether the saved profile is applied at start-up is
            # the user's setting — not something to override from here.
            if not recovered:
                gpu_overclock.apply_oc(core_offset_mhz=int(oc.get("core", 0)),
                                       mem_offset_mhz=int(oc.get("mem", 0)),
                                       power_pct=int(oc.get("power", 100)))
            guid = str(r.get("power_guid") or "")
            if len(guid) == 36 and guid.count("-") == 4:
                run_cmd(["powercfg", "/setactive", guid], timeout=6)
            log.info("  Max-FPS target released: restored saved OC + prior power plan")
        elif target == "low_latency":
            if r.get("we_enabled_latency"):
                from core import competitive_latency
                competitive_latency.reset()
                log.info("  Low-Latency target released: reverted Competitive Latency")
    except Exception as e:
        log.warning(f"restore_target failed: {e}")


def restore_normal_mode(reason: str = "game exit"):
    """Revert ALL hard tweaks back to the saved baseline — guaranteed normal state."""
    with _session_lock:
        _restore_normal_mode(reason)


def _restore_normal_mode(reason: str):
    log.info(f"Restoring normal mode (reason: {reason})...")
    _exit_seen_ts = time.time()      # for crash detection, which runs after all of this
    # beta.13 — reverse any per-game optimization-target posture first
    # (GPU OC / power plan / competitive latency), before the generic tweaks.
    restore_target()
    # The game's processes that are still running (game mode switched off
    # mid-game): priority, affinity and throttling back as they were.  After
    # a real exit they are gone — and a pid may be somebody else's by now,
    # which the creation time tells apart.
    procs = dict(_engine.get("game_procs") or {})
    if not procs and _engine.get("proc_prior") and _engine.get("active_pid"):
        procs = {_engine["active_pid"]: {"prior": _engine["proc_prior"], "created": None}}
    game_pids = set(procs)
    _engine["proc_prior"] = None
    _engine["game_procs"] = {}
    if reason != "game exit":
        for pid, rec in procs.items():
            if not rec.get("prior") or process_tuning.alive(pid, rec.get("created")) is False:
                continue
            try:
                process_tuning.release(pid, rec["prior"])
            except Exception as e:
                log.debug(f"could not release game process {pid}: {e}")
    # Everything else changed for the session — the registry values, and the
    # power-plan settings on the plan they were changed on — back to exactly
    # what it was before.
    recorded_session = bool(reg_originals.noted("gamemode_plan_recorded"))
    recorded_registry = bool(reg_originals.noted("gamemode_reg_recorded"))
    reg_originals.forget_notes("gamemode_plan_recorded", "gamemode_reg_recorded")
    plan = reg_originals.restore_all(_SCOPE)
    if plan["restored"] or plan["failed"]:
        log.info(f"  Restored {plan['restored']} setting(s) to what they were before the game"
                 + (f", {plan['failed']} could not be" if plan["failed"] else ""))
    # Capture the game name BEFORE we clear engine state, so we can use it in the notification.
    _prev_game = _engine.get("active_game")
    baseline = _load_baseline()

    # If we have no baseline, use sensible Windows defaults
    priority_sep = baseline.get("priority_sep", "2")  # 0x02 = Windows default
    sys_resp = baseline.get("sys_responsiveness", "14")  # 0x14 = 20 decimal = default
    net_throttle = baseline.get("net_throttle", "a")  # 0x0a = 10 = default
    cpu_idle_was_disabled = baseline.get("cpu_idle_disabled", False)

    # 1-3. Registry values, from the baseline — only for a session started by
    #      a build that did not record them (they came back above otherwise).
    if not recorded_registry:
        val = int(priority_sep, 16)
        run_cmd(["reg","add",r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
                 "/v","Win32PrioritySeparation","/t","REG_DWORD","/d",str(val),"/f"])
        log.info(f"  Restored Win32PrioritySeparation -> 0x{priority_sep}")
        val2 = int(sys_resp, 16)
        run_cmd(["reg","add",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
                 "/v","SystemResponsiveness","/t","REG_DWORD","/d",str(val2),"/f"])
        log.info(f"  Restored SystemResponsiveness -> {val2}")
        val3 = int(net_throttle, 16)
        run_cmd(["reg","add",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
                 "/v","NetworkThrottlingIndex","/t","REG_DWORD","/d",str(val3),"/f"])
        log.info(f"  Restored NetworkThrottlingIndex -> {val3}")

    # 4. Restore CPU idle — only for a session started by a build that kept
    #    no records (otherwise it was put back above, or has since become the
    #    optimizer's own setting and is not ours to touch).
    if not recorded_session and not cpu_idle_was_disabled:
        run_cmd(["powercfg","/setacvalueindex","SCHEME_CURRENT","SUB_PROCESSOR","IdleDisable","0"])
        run_cmd(["powercfg","/setactive","SCHEME_CURRENT"])
        log.info("  Restored CPU idle -> enabled")

    # 5. Remove any DSCP game rules (including ones created under the
    #    app's pre-rename name).
    run_ps(f'Get-NetQosPolicy | Where-Object {{{qos_policy_filter("Game_")}}} | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue')
    log.info("  Removed DSCP game rules")

    # 5b. Remove streaming DSCP rules & restore streamer process priority to Normal
    run_ps(f'Get-NetQosPolicy | Where-Object {{{qos_policy_filter("Stream_")}}} | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue')
    # Streaming apps: each process game mode raised goes back to the class it
    # had — not to Normal for everyone with that name, which also lowered
    # copies the user had raised themselves.
    raised = dict(_engine.get("streamer_prior") or {})
    for pid, before in raised.items():
        try:
            process_tuning.set_priority(pid, before)
        except Exception as e:
            log.debug(f"could not put streaming process {pid} back: {e}")
    _engine["streamer_prior"] = {}
    _engine["streaming_exes"] = []
    if raised:
        log.info(f"  Put {len(raised)} streaming process(es) back to their own priority")
    # Background programs out of efficiency mode.
    try:
        _eco_restore()
    except Exception as e:
        log.debug(f"efficiency-mode restore failed: {e}")

    # v3 — capture exe + active OC BEFORE we wipe engine state, so the
    # crash-recovery handler has the data it needs.
    _prev_exe       = _engine.get("active_exe")
    _prev_oc_core   = int(_engine.get("active_oc_core", 0) or 0)
    _prev_oc_mem    = int(_engine.get("active_oc_mem", 0) or 0)

    _clear_gaming_flag()
    _engine["gaming_mode"] = False
    _engine["changes"] = []
    _engine["checks"] = []
    _engine["active_game"] = None
    _engine["active_exe"] = None
    _engine["active_pid"] = None
    _engine["game_image"] = ""
    _engine["power_applied"] = []
    _engine["power_scheme"] = ""
    _engine["power_limits"] = {}
    _engine["exit_ticks"] = 0
    _engine["active_oc_core"] = 0
    _engine["active_oc_mem"]  = 0
    log.info("  Normal mode restored")

    # v3.4 — restore Vispora efficiency mode now that the game is
    # gone.  Saves laptop battery + lets foreground apps have CPU again.
    # Skipped if the gamepad mapper master switch is still on (the user
    # might still be using mappings without an active game).
    try:
        from core import gamepad_mapper as _gm
        mapper_busy = bool(_gm._load_state().get("enabled"))
    except Exception:
        mapper_busy = False
    if not mapper_busy:
        try:
            import sys
            main_app = sys.modules.get("app") or sys.modules.get("__main__")
            if main_app and hasattr(main_app, "_set_efficiency_mode"):
                main_app._set_efficiency_mode()
        except Exception as e:
            log.debug(f"could not restore efficiency mode: {e}")

    # 5b. Background pauser — resume any AI apps / browsers we suspended.
    #     Always run this regardless of `reason` so manual reset / boot
    #     recovery never leaves a process frozen.
    try:
        from core import background_pauser
        bp_settings = background_pauser.get_settings()
        # Deliberately not gated on the pauser still being enabled: switching
        # it off mid-game must not strand what it already froze.  resume_now()
        # is a no-op when nothing is recorded as paused.
        if bp_settings.get("auto_resume_on_game_exit", True):
            r = background_pauser.resume_now(reason=f"restore_normal: {reason}")
            if r.get("resumed"):
                log.info(f"  Resumed {len(r['resumed'])} background apps")
    except Exception as e:
        log.debug(f"background pauser resume hook failed: {e}")

    # 6. Post-game refresh — clean up RAM/caches/orphan processes so the PC is
    #    fresh for the next game session. Done in a background thread so the
    #    notification fires immediately and we don't block.
    if reason in ("game exit",):
        threading.Thread(target=_post_game_refresh, args=(_prev_game, game_pids), daemon=True).start()

    # v3 — release the perf sampler's game-mode consumer slot.  It
    # keeps running if hwmon-page or adaptive-tuning are still using it.
    try:
        from core import perf_monitor
        perf_monitor.stop(consumer="game-mode")
    except Exception as e:
        log.debug(f"perf_monitor stop failed: {e}")

    # v3 — crash detection THEN adaptive-tuning session update.  Run in
    # a background thread so the restore_normal_mode call returns
    # immediately (event-log query takes 1-2s).  Notification fires
    # within ~5s of game exit.  Only on real game exits — boot-recovery
    # and user-quit reasons skip this.
    if reason in ("game exit",) and _prev_exe:
        def _post_exit_chain():
            was_crash = False
            try:
                from core import crash_recovery
                cr_result = crash_recovery.handle_game_exit(
                    exe=_prev_exe,
                    display_name=_prev_game,
                    active_core=_prev_oc_core,
                    active_mem=_prev_oc_mem,
                    exit_ts=_exit_seen_ts,
                )
                was_crash = bool(cr_result.get("crashed"))
            except Exception as e:
                log.warning(f"crash_recovery.handle_game_exit failed: {e}")
            # Adaptive Tuning learns from this session's outcome
            try:
                from core import adaptive_tuning
                adaptive_tuning.on_game_exit(
                    exe=_prev_exe,
                    display_name=_prev_game,
                    was_crash=was_crash,
                )
            except Exception as e:
                log.debug(f"adaptive_tuning.on_game_exit failed: {e}")
        threading.Thread(target=_post_exit_chain, daemon=True,
                         name="crash-recovery").start()

    # Notify only on real exits, not on manual/boot-recovery resets.
    if _prev_game and _notifier and reason in ("game exit",):
        try:
            _notifier.notify_game_exited(_prev_game)
        except Exception as e:
            log.debug(f"Notifier failed: {e}")


# Helper processes a game starts and can leave running after it exits.  Only
# ever stopped when the process is a CHILD of the game that has just exited.
# (This used to stop every process of these names — Steam's and the Epic
# launcher's own web helpers, SteamVR, a crash reporter the user was about to
# send — and it ran whenever the monitor believed a game had exited, which
# under load it sometimes did while the game was still running.)
_GAME_LEFTOVERS = frozenset(n.lower() for n in (
    "UnrealCEFSubProcess.exe", "ShaderCompileWorker.exe", "ShaderCompileWorker-Win64-Shipping.exe",
    "EasyAntiCheat_Setup.exe", "ScreenshotUploader.exe",
))


def _stop_game_leftovers(game_pids) -> int:
    pids = {int(p) for p in (game_pids or ())}
    if not pids:
        return 0
    stopped = 0
    for p in process_tuning.processes() or []:
        if p["ppid"] in pids and p["pid"] not in pids and p["exe"].lower() in _GAME_LEFTOVERS:
            if process_tuning.terminate(p["pid"]):
                stopped += 1
                log.info(f"  Stopped {p['exe']} (PID {p['pid']}), left running by the game")
    return stopped


def _post_game_refresh(game_name: str = None, game_pids=None):
    """Refresh the PC state after a game exits — frees RAM, stops what the game
    left running, flushes caches, so the next game launches into a clean state.

    Runs in a background thread; does not block the caller.
    """
    started = time.time()
    log.info(f"=== Post-game refresh starting (after: {game_name or 'game'}) ===")
    freed_total = 0

    # 1. Drop RAM working sets + clear standby list (the big win — usually frees 1-4 GB)
    if _cleaner is not None:
        try:
            r = _cleaner.clean_ram()
            if r.get("ok") and r.get("freed"):
                freed_total += r["freed"]
                log.info(f"  RAM refresh freed: {r.get('freed_str', '?')}")
        except Exception as e:
            log.warning(f"  RAM clean failed: {e}")

    # 2. Flush DNS cache — drops stale entries from in-game match servers
    try:
        run_cmd(["ipconfig", "/flushdns"], timeout=10)
        log.info("  DNS cache flushed")
    except Exception as e:
        log.debug(f"  DNS flush failed: {e}")

    # 3. Stop helper processes the game itself started and left running.
    try:
        _stop_game_leftovers(game_pids)
    except Exception as e:
        log.debug(f"  leftover check failed: {e}")

    # 4. Reset MMCSS gaming task scheduler state — bounces the multimedia
    #    scheduler so the next game gets a clean priority slot.
    try:
        run_ps("Restart-Service -Name 'MMCSS' -Force -ErrorAction SilentlyContinue", timeout=8)
        log.info("  MMCSS scheduler refreshed")
    except Exception:
        pass

    # (A step here used to delete the *.lock and *.tmp files in the NVIDIA,
    # AMD and DirectX shader caches after every game.  Those files are the
    # driver's own bookkeeping, still in use by it — and by a game that was
    # restarting itself.  Pulling them out corrupted the cache, which shows up
    # in games as visual glitches until the cache is rebuilt.  The driver
    # manages its cache; it is left alone.)

    # 6. Trigger Windows page combine to recompact RAM after working set drop
    try:
        run_ps("[System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers(); [System.GC]::Collect()", timeout=5)
    except Exception:
        pass

    # (A step here used to stop every windowless rundll32 started in the last
    # ten minutes — Windows runs maintenance, printing and control-panel work
    # in those.  It did nothing for the next game, and is gone.)

    duration = time.time() - started
    log.info(f"=== Post-game refresh done in {duration:.1f}s ===")
    if _notifier and freed_total > 0:
        try:
            mb = freed_total / (1024 * 1024)
            _notifier.toast(
                "Vispora — PC Refreshed",
                f"Freed {mb:.0f} MB RAM, flushed caches. Ready for the next game.",
            )
        except Exception:
            pass


def check_and_restore_on_boot():
    """Called on app startup — if gaming flag is set, we crashed/rebooted mid-game. Restore normal."""
    # First, capture baseline if we don't have one yet
    if not os.path.exists(BASELINE_FILE):
        log.info("Capturing initial system baseline (first run)...")
        baseline = _capture_baseline()
        _save_baseline(baseline)
        log.info(f"  Baseline saved: {baseline}")

    # Check if gaming tweaks are stuck from a previous session
    if _is_gaming_flag_set():
        log.info("Gaming flag detected from previous session — reverting to normal mode")
        restore_normal_mode("boot recovery")
    else:
        # Even if no flag, verify the system is actually in normal state
        # by checking key values against baseline
        baseline = _load_baseline()
        if baseline:
            _verify_normal_state(baseline)
    if os.path.exists(_TARGET_RESTORE_FILE):
        restore_target()        # a per-game posture that outlived the session that set it
    if os.path.exists(_ECO_FILE):
        _eco_restore()          # background programs left in efficiency mode by a crash


def _verify_normal_state(baseline: dict):
    """Check if system settings match baseline; fix if they don't."""
    r = run_cmd(["reg","query",r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl","/v","Win32PrioritySeparation"])
    if r["ok"] and "0x" in r["out"]:
        current = r["out"].split("0x")[-1].strip().split()[0].lower()
        expected = baseline.get("priority_sep", "2").lower()
        if current == "28" and expected != "28":
            # Gaming tweaks are still active!
            log.info(f"Win32PrioritySeparation is 0x28 (gaming) but baseline is 0x{expected} — restoring")
            restore_normal_mode("stale gaming tweak detected")
            return

    # Check CPU idle
    # Only against a baseline whose idle state was really read (see
    # _cpu_idle_disabled): older ones say "enabled" unconditionally.
    idle_disabled = baseline.get("idle_probe") == "qh" and _cpu_idle_disabled()
    baseline_idle = baseline.get("cpu_idle_disabled", False)
    if idle_disabled and not baseline_idle:
        log.info("CPU idle is disabled but baseline says enabled — restoring")
        restore_normal_mode("stale CPU idle tweak detected")


def recapture_baseline():
    """Manually recapture the baseline (e.g. after user applies permanent tweaks they want to keep)."""
    if _engine["gaming_mode"] or _is_gaming_flag_set():
        # What is on the system right now is the gaming profile, not the
        # user's normal state.  Saving it as the baseline would make every
        # later "restore" write the gaming values back.
        log.warning("Baseline recapture refused — a gaming profile is applied")
        return {"ok": False, "err": "A game profile is active — close the game first."}
    log.info("Recapturing system baseline...")
    baseline = _capture_baseline()
    _save_baseline(baseline)
    log.info(f"  New baseline: {baseline}")
    return {"ok": True, "baseline": baseline}


def refresh_baseline_after_reset() -> None:
    """Reset All has just put these values back, so what is on the system now
    IS the normal state — even with a game open.  Without this, closing that
    game (or the next start-up check) wrote the pre-reset values back."""
    baseline = _capture_baseline()
    _save_baseline(baseline)
    log.info(f"  Baseline refreshed after reset: {baseline}")


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _is_game(proc_name: str) -> bool:
    name = proc_name.lower()
    if not name.endswith(".exe"): name += ".exe"
    # Streaming software never triggers gaming mode on its own — it's a companion
    if name in STREAMING_EXES: return False
    if name in KNOWN_GAME_EXES: return True
    if name in _IGNORE_PROCS: return False
    for hint in _GAME_HINTS:
        if hint in name: return True
    return False


def _find_running_streaming_exes() -> list[dict]:
    """Return list of {exe, pid} for streaming apps currently running."""
    return [{"exe": p["exe"].lower(), "pid": p["pid"]}
            for p in (process_tuning.processes() or []) if p["exe"].lower() in STREAMING_EXES]


def _nice_name(exe: str) -> str:
    name = exe.replace(".exe","").replace("-Win64-Shipping","").replace("_"," ")
    result = ""
    for i, c in enumerate(name):
        if c.isupper() and i > 0 and name[i-1].islower(): result += " "
        result += c
    return result.strip().title()


# ═══════════════════════════════════════════════════════════════
# Monitor Loop
# ═══════════════════════════════════════════════════════════════
# v2.9.0 — multi-executable disambiguation.
# Several scenarios produce more than one matching game process:
#   • A launcher (Steam.exe, EpicGamesLauncher.exe) plus the actual game
#   • Two games running side by side (rare but possible)
#   • A game's main exe plus a child renderer/anti-cheat process that
#     matches a custom-added rule
# The old code took whatever Get-Process happened to enumerate first.
# Now we collect ALL candidates, score each by:
#     +1000  process owns the foreground window  (user is actively playing)
#     + working-set MB / 100   (real games use 1-4 GB; launchers use 100-500 MB)
#     -200   exe is on the streaming/launcher ignore list
# and pick the highest score.

def _get_foreground_pid() -> int:
    """Return the PID of the process that currently owns the foreground
    window (the window the user has focused), or 0 if it can't be read."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value or 0)
    except Exception:
        return 0


def _scan_game_candidates() -> list[dict]:
    """Return a list of candidate game processes with scoring data.

    Each entry: {pid, exe, name, ws_mb, foreground, score}
    Only processes with a visible main window are considered — a real
    game always has one.
    """
    # Pull PID, ProcessName, and WorkingSet64 (in MB) in a single PS call.
    # Filter out background processes so we don't waste time on services.
    r = run_ps(
        "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } "
        "| Select-Object Id,ProcessName,@{N='WS';E={[int]($_.WorkingSet64/1MB)}} "
        "| ConvertTo-Json -Compress",
        timeout=10,
    )
    if not (r["ok"] and r["out"]):
        return []
    try:
        procs = json.loads(r["out"])
    except json.JSONDecodeError:
        return []
    if not isinstance(procs, list):
        procs = [procs]

    fg_pid = _get_foreground_pid()
    candidates = []
    for p in procs:
        pname = (p.get("ProcessName") or "").strip()
        pid = p.get("Id", 0)
        ws_mb = int(p.get("WS") or 0)
        if not pname or not pid:
            continue
        exe = pname + ".exe"
        if not _is_game(exe):
            continue
        score = ws_mb / 100.0          # base score from memory footprint
        if pid == fg_pid:
            score += 1000              # foreground always wins
        # Penalise known launchers / streaming apps if they somehow matched
        if exe.lower() in STREAMING_EXES:
            score -= 200
        candidates.append({
            "pid":        pid,
            "exe":        exe,
            "name":       _nice_name(exe),
            "ws_mb":      ws_mb,
            "foreground": (pid == fg_pid),
            "score":      round(score, 1),
        })
    # Sort highest-score first so callers can just take [0].
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _live_game_pids() -> list:
    """The processes of the tracked game's exe that are running now.  Read
    through the API, which cannot time out: the PowerShell check this used to
    be took over five seconds under load now and then, and a timeout was
    taken for "the game has exited" — normal mode came back mid-game, and the
    whole profile was applied again a minute later."""
    exe = _engine.get("active_exe") or ""
    pids = process_tuning.find_image(_engine.get("game_image") or "", exe)
    pid = _engine.get("active_pid")
    if not pids and pid:
        rec = (_engine.get("game_procs") or {}).get(pid) or {}
        if process_tuning.alive(pid, rec.get("created"), exe) is not False:
            pids = [pid]        # (cannot be told counts as running: only a definite exit ends a session)
    return pids


def _log_checks(prefix: str = "") -> None:
    checks = _engine.get("checks") or []
    good = sum(1 for c in checks if c["ok"])
    log.info(f"  {prefix}{good} of {len(checks)} game-mode settings in effect (read back)")
    for c in checks:
        if not c["ok"]:
            log.info(f"    {'not done' if c['ok'] is False else 'unknown'}: {c['text']}")


def _start_session(best: dict, gen: int) -> None:
    with _session_lock:
        if _engine["gaming_mode"] or not _engine["monitoring"] or _engine.get("monitor_gen", 0) != gen:
            return          # another monitor thread has already started it, or this one is being replaced
        pid, exe, display = best["pid"], best["exe"], best["name"]
        fg = " [foreground]" if best.get("foreground") else ""
        log.info(f"Game detected: {display} ({exe}, PID {pid}, {best.get('ws_mb', 0)} MB{fg}, "
                 f"score {best.get('score', 0)})")
        # The baseline is only a fallback now, for a session left behind by a
        # build that kept no records.  Never taken while a session's gaming
        # values may still be in place.
        if not _is_gaming_flag_set():
            _save_baseline(_capture_baseline())
        ident = process_tuning.identity(pid)
        _engine.update(active_game=display, active_exe=exe, active_pid=pid, game_image=ident.get("image") or "",
                       game_procs={}, checks=[], changes=[], power_applied=[], power_limits={}, streamer_prior={},
                       exit_ticks=0, tick=0, proc_prior=None, eco_seen={}, eco_failed=0)
        layout = process_tuning.cpu_layout()
        _engine["eco_on"] = bool(_load_gp_settings().get("background_on_ecores", True) and layout.get("hybrid"))
        _engine["eco_session"] = process_tuning.session_of(pid)
        _engine["gaming_mode"] = True
        _set_gaming_flag()
        reg_originals.note("gamemode_reg_recorded")
        # Fire the notification BEFORE the tweaks — user sees it immediately.
        if _notifier:
            try:
                _notifier.notify_game_detected(display, exe)
            except Exception as e:
                log.debug(f"Notifier failed: {e}")

        # 1. The game's own processes first — what Task Manager shows, done in
        #    milliseconds — then the system-wide values.
        _tune_game_processes(process_tuning.find_image(_engine["game_image"], exe) or [pid])
        # Then everything else onto the E-cores — whether or not the game's
        # anti-cheat let the settings above through (on Call of Duty it does
        # not, and the game still gains the P-cores).
        if _engine["eco_on"]:
            _eco_sweep()
        elif not layout.get("hybrid"):
            log.info("  Background programs left where they are: this CPU has no efficiency cores")
        _apply_registry_tweaks()
        _apply_session_extras(exe)

        # v3 — snapshot the OC offsets that are active right now
        # so crash_recovery can blacklist them if the game crashes.
        try:
            from core import gpu_overclock
            st = gpu_overclock.read_live_state()
            # We want the OFFSETS (over stock baseline), not raw clocks
            bl = gpu_overclock.get_stock_baseline() or {}
            if st.get("ok") and bl:
                _engine["active_oc_core"] = max(
                    0, int(st.get("core_max_mhz", 0))
                    - int(bl.get("core_max_mhz", st.get("core_max_mhz", 0))))
                _engine["active_oc_mem"]  = max(
                    0, int(st.get("mem_app_mhz", 0))
                    - int(bl.get("mem_app_mhz", st.get("mem_app_mhz", 0))))
            else:
                # Fallback: read from saved profile
                p = gpu_overclock.load_profile()
                if p.get("exists"):
                    _engine["active_oc_core"] = int(p.get("core_offset_mhz", 0))
                    _engine["active_oc_mem"]  = int(p.get("mem_offset_mhz", 0))
                else:
                    _engine["active_oc_core"] = 0
                    _engine["active_oc_mem"]  = 0
            log.info(f"  Captured active OC at game-start: "
                     f"core+{_engine['active_oc_core']} / "
                     f"mem+{_engine['active_oc_mem']}")
        except Exception as e:
            log.debug(f"Could not capture active OC: {e}")
            _engine["active_oc_core"] = 0
            _engine["active_oc_mem"]  = 0
        # v3 — start the perf sampler so we have data ready
        # for crash diagnostics and Adaptive Tuning.
        try:
            from core import perf_monitor
            perf_monitor.start(consumer="game-mode")
        except Exception as e:
            log.debug(f"perf_monitor start failed: {e}")

        # beta.13 — apply this game's optimization TARGET posture
        # (max_fps / low_latency) BEFORE AT engages.  For targeted
        # games AT is auto-disabled, so on_game_start below no-ops
        # for them and won't fight the posture we just set.
        try:
            apply_target(exe)
        except Exception as e:
            log.debug(f"apply_target failed: {e}")

        # Power-plan tweaks after the target, so they land on the plan the
        # target has just selected (see _apply_power_tweaks).
        try:
            _apply_power_tweaks()
        except Exception as e:
            log.debug(f"power-plan tweaks failed: {e}")

        # The slow part last: network priority (PowerShell) and streaming apps.
        try:
            _apply_network_priority(exe, _find_running_streaming_exes())
        except Exception as e:
            log.debug(f"network priority failed: {e}")

        # v3 — engage Adaptive Tuning if this game is opted in.
        # AT applies its own learned offsets (overriding the
        # current OC) and starts the per-game tuning loop.
        try:
            from core import adaptive_tuning
            at_result = adaptive_tuning.on_game_start(exe, display)
            if at_result.get("engaged"):
                log.info(f"  Adaptive Tuning engaged: starting at "
                         f"core+{at_result['starting_core']}/"
                         f"mem+{at_result['starting_mem']}")
                # Update our OC snapshot now that AT applied its own
                _engine["active_oc_core"] = at_result["starting_core"]
                _engine["active_oc_mem"]  = at_result["starting_mem"]
        except Exception as e:
            log.debug(f"adaptive_tuning.on_game_start failed: {e}")
        _log_checks()


def _switch_game(new: dict) -> None:
    """Another known game is still running: track that one.  The system-wide
    tweaks carry over; the per-process ones go to the new game's processes."""
    log.info(f"  Other known game(s) still running: switching tracker to {new['name']} (PID {new['pid']})")
    old_exe, old_game = _engine.get("active_exe", ""), _engine.get("active_game")
    ident = process_tuning.identity(new["pid"])
    _engine.update(active_pid=new["pid"], active_exe=new["exe"], active_game=new["name"],
                   game_image=ident.get("image") or "", game_procs={}, exit_ticks=0, proc_prior=None)
    _engine["checks"] = [c for c in _engine["checks"] if not c["key"].startswith("proc:") and c["key"] != "waiting"]
    _tune_game_processes(process_tuning.find_image(_engine["game_image"], new["exe"]) or [new["pid"]])
    # Notify AT so it can switch its target too
    try:
        from core import adaptive_tuning as _at
        _at.on_game_exit(old_exe, old_game, was_crash=False, reason="game-mode tracker switch")
        _at.on_game_start(new["exe"], new["name"])
    except Exception as e:
        log.debug(f"AT switch failed: {e}")


def _session_tick() -> None:
    """One monitor tick during a game: follow the game's processes, keep what
    game mode set in effect, and end the session only on a definite exit."""
    _engine["tick"] = _engine.get("tick", 0) + 1
    exe = _engine.get("active_exe") or "the game"
    pids = _live_game_pids()
    if pids:
        if _engine.get("exit_ticks"):
            log.info(f"  {exe} is running again (PID {', '.join(map(str, pids))}) — game mode stays on")
            _engine["checks"] = [c for c in _engine["checks"] if c["key"] != "waiting"]
            _engine["changes"] = [c["text"] for c in _engine["checks"]]
        _engine["exit_ticks"] = 0
        if _engine.get("active_pid") not in pids:
            log.info(f"  {exe}: following its new process (PID {_engine.get('active_pid')} -> {pids[0]})")
            _engine["active_pid"] = pids[0]
        recheck = _engine["tick"] % _SYSTEM_CHECK_TICKS == 0
        _tune_game_processes(pids, recheck=recheck)
        _eco_tick()
        if recheck:
            _apply_registry_tweaks(recheck=True)
            _apply_power_tweaks(recheck=True)
        return
    # No process of the game is running any more.
    _engine["exit_ticks"] = _engine.get("exit_ticks", 0) + 1
    if _engine["exit_ticks"] == 1:
        log.info(f"Tracked game exited: {_engine.get('active_game') or 'Unknown'} (PID {_engine.get('active_pid')})")
        others = _scan_game_candidates()
        if others:
            _switch_game(others[0])
            return
        # Some games close and start again (an update, a restart after the
        # first launch, an anti-cheat handshake).  Normal mode used to come
        # back in between, and the game ran a minute without the profile.
        _check("waiting", None, "Game closed — game mode stays on a few seconds in case it restarts")
    elif _engine["exit_ticks"] >= _EXIT_GRACE_TICKS:
        _restore_normal_mode("game exit")


def _monitor_loop(gen: int = 0):
    log.info("Game monitor started")

    while _engine["monitoring"] and _engine.get("monitor_gen", 0) == gen:
        try:
            if _engine["gaming_mode"]:
                with _session_lock:
                    if _engine["gaming_mode"] and _engine["monitoring"] and _engine.get("monitor_gen", 0) == gen:
                        _session_tick()
            else:
                # No active game — scan for candidates and pick the best one.
                candidates = _scan_game_candidates()
                if candidates:
                    if len(candidates) > 1:
                        # Help the user understand why we picked X instead of Y
                        runners_up = ", ".join(
                            f"{c['name']} (PID {c['pid']}, {c['ws_mb']} MB, score {c['score']})"
                            for c in candidates[1:5]
                        )
                        log.info(
                            f"Multiple game candidates running ({len(candidates)}). "
                            f"Other matches: {runners_up}"
                        )
                    _start_session(candidates[0], gen)
        except Exception as e:
            log.warning(f"Monitor error: {e}")
        time.sleep(_TICK_S)

    if _engine.get("monitor_gen", 0) != gen:
        log.info("Game monitor thread superseded by a newer one — exiting quietly")
        return          # the newer thread owns the state; nothing here to clean up
    # Cleanup on stop
    if _engine["gaming_mode"]:
        restore_normal_mode("monitor stopped")
    log.info("Game monitor stopped")


# ═══════════════════════════════════════════════════════════════
# Custom game management
# ═══════════════════════════════════════════════════════════════
def _load_custom_exes():
    p = os.path.join(PROFILES_DIR, "custom_exes.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                for c in json.load(f):
                    KNOWN_GAME_EXES.add(c["exe"].lower())
        except Exception: pass

_load_custom_exes()


def add_custom_game(exe_name: str, display_name: str = "") -> dict:
    """Add a game to the local custom-games list.

    v3.3 — also fires a best-effort POST to the Railway community
    endpoint so OTHER Vispora users get this exe in their known
    games list automatically.  Opt-out via the Community settings panel."""
    exe_name = exe_name.lower().strip()
    if not exe_name.endswith(".exe"): exe_name += ".exe"
    # Community submission (fire-and-forget; never blocks the local add)
    try:
        from core import community
        nice = display_name or _nice_name(exe_name)
        community.submit_game(exe_name, nice)
    except Exception as e:
        log.debug(f"community submit hook failed: {e}")
    p = os.path.join(PROFILES_DIR, "custom_exes.json")
    custom = []
    if os.path.exists(p):
        try:
            with open(p) as f: custom = json.load(f)
        except Exception as e:
            # v3 — log + back up the corrupted file instead of silently
            # dropping the user's custom-game list.  An OS crash mid-
            # write would otherwise wipe everything they'd added.
            try:
                bak = p + f".corrupt-{int(time.time())}.json"
                os.replace(p, bak)
                log.warning(f"custom_exes.json was corrupted ({e}), backed up to {bak}")
            except Exception: pass
    entry = {"exe": exe_name, "name": display_name or _nice_name(exe_name)}
    if not any(c["exe"] == exe_name for c in custom):
        custom.append(entry)
        KNOWN_GAME_EXES.add(exe_name)
    # v3 — atomic write: stage to .tmp, fsync, rename.  Without this a
    # crash between truncate and write produces a zero-byte file and
    # all custom games disappear at next launch.
    _atomic_write_json(p, custom)
    log.info(f"Added custom game: {exe_name}")
    return {"ok": True, "exe": exe_name}


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        try:
            f.flush()
            os.fsync(f.fileno())
        except Exception: pass
    os.replace(tmp, path)


def remove_custom_game(exe_name: str) -> dict:
    p = os.path.join(PROFILES_DIR, "custom_exes.json")
    if not os.path.exists(p): return {"ok": False, "err": "No custom games"}
    try:
        with open(p) as f: custom = json.load(f)
        custom = [c for c in custom if c["exe"] != exe_name.lower()]
        _atomic_write_json(p, custom)
        KNOWN_GAME_EXES.discard(exe_name.lower())
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def get_all_profiles() -> dict:
    profiles = {"known_games": {"name": "Auto-Detected Games", "count": len(KNOWN_GAME_EXES)}}
    p = os.path.join(PROFILES_DIR, "custom_exes.json")
    if os.path.exists(p):
        try:
            with open(p) as f: custom = json.load(f)
            profiles["custom"] = {"name": "Custom Games", "exes": custom, "count": len(custom)}
        except Exception: pass
    return profiles


# ═══════════════════════════════════════════════════════════════
# Engine Control
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# v2.9.9.6 — game-mode auto-enable on boot
# ═══════════════════════════════════════════════════════════════════════════
# Persists a `auto_enable_on_boot` flag in APPDATA so the user can opt out
# from the Settings page.  app.py.main() calls auto_enable_on_boot_if_set()
# after the boot-prep delay so we don't fight Windows for resources during
# login.  When enabled, the game-detection monitor starts automatically
# and any game from the (now ~280-entry) database gets the gaming profile
# applied as soon as it launches.
import os as _os
_GAME_PROFILES_SETTINGS_PATH = _os.path.join(APPDATA_DIR, "game_profiles_settings.json")

_DEFAULT_GP_SETTINGS = {
    "auto_enable_on_boot": True,
    # Lift any CPU-core restriction on the game (P-cores AND E-cores).  The
    # switch is for the rare game that misbehaves when handed every core.
    "all_cores": True,
    # While a game runs, the other programs in efficiency mode, so on a CPU
    # with E-cores they go there and the game has the P-cores.
    "background_on_ecores": True,
}


def _load_gp_settings() -> dict:
    if not _os.path.exists(_GAME_PROFILES_SETTINGS_PATH):
        return dict(_DEFAULT_GP_SETTINGS)
    try:
        with open(_GAME_PROFILES_SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return {**_DEFAULT_GP_SETTINGS, **d}
    except Exception:
        pass
    return dict(_DEFAULT_GP_SETTINGS)


def _save_gp_settings(d: dict):
    atomic_write_json(_GAME_PROFILES_SETTINGS_PATH, d)


def get_settings() -> dict:
    return _load_gp_settings()


def set_settings(auto_enable_on_boot=None, all_cores=None, background_on_ecores=None) -> dict:
    s = _load_gp_settings()
    if auto_enable_on_boot is not None:
        s["auto_enable_on_boot"] = bool(auto_enable_on_boot)
    if all_cores is not None:
        s["all_cores"] = bool(all_cores)
    if background_on_ecores is not None:
        s["background_on_ecores"] = bool(background_on_ecores)
    _save_gp_settings(s)
    return {"ok": True, "settings": s}


def auto_enable_on_boot_if_set() -> dict:
    """Called once from app.py.main() after the boot-prep delay.

    If the user has `auto_enable_on_boot=true` (default), starts the
    game-detection monitor.  Idempotent — if monitoring is already on,
    returns ok without restarting.
    """
    s = _load_gp_settings()
    if not s.get("auto_enable_on_boot", True):
        log.info("game-mode auto-enable: disabled in settings")
        return {"ok": True, "started": False, "reason": "disabled in settings"}
    if _engine.get("monitoring"):
        log.info("game-mode auto-enable: already monitoring")
        return {"ok": True, "started": False, "reason": "already running"}
    log.info(f"game-mode auto-enabling on boot (database has {len(KNOWN_GAME_EXES)} known games)")
    return start_monitoring()


def start_monitoring() -> dict:
    if _engine["monitoring"]: return {"ok": True, "msg": "Already monitoring"}
    _engine["monitoring"] = True
    # Each loop gets a generation number and exits when it is no longer the
    # current one.  Off→on within the loop's 3 s sleep used to leave the old
    # thread running beside the new one; at the next game launch one could
    # capture a "baseline" while the other was half-way through applying the
    # gaming tweaks — and that poisoned baseline was then restored forever.
    _engine["monitor_gen"] = _engine.get("monitor_gen", 0) + 1
    t = threading.Thread(target=_monitor_loop, args=(_engine["monitor_gen"],), daemon=True)
    t.start()
    _engine["thread"] = t
    return {"ok": True}


def stop_monitoring() -> dict:
    _engine["monitoring"] = False
    return {"ok": True}


def get_engine_status() -> dict:
    return {
        "monitoring": _engine["monitoring"],
        "gaming_mode": _engine["gaming_mode"],
        "active_game": _engine["active_game"],
        "active_exe": _engine["active_exe"],
        "active_pid": _engine["active_pid"],
        "applied_changes": _engine["changes"],
        # What is in effect as last read back — {"ok": True | False | None, "text"} each.
        "checks": [{"ok": c["ok"], "text": c["text"]} for c in _engine.get("checks", [])],
        "verified_at": _engine.get("verified_at", 0.0),
        "game_pids": sorted(_engine.get("game_procs") or {}),
        "waiting_for_restart": bool(_engine.get("exit_ticks")),
        "known_game_count": len(KNOWN_GAME_EXES),
        "streaming_count": len(STREAMING_EXES),
        "streaming_prioritized": _engine.get("streaming_exes", []),
        "baseline_exists": os.path.exists(BASELINE_FILE),
        "gaming_flag_set": _is_gaming_flag_set(),
    }


def get_active_game_info() -> dict | None:
    """Returns the currently-active game's identification + the OC offsets
    captured at game-start, or None if no game is being tracked.

    Used by adaptive_tuning to retroactively engage when the AT
    settings flip on while a game is already running.  Note that this
    returns the SINGLE game game-mode is currently optimizing for;
    when multiple games are running simultaneously, use
    get_active_games() to see all of them."""
    if not _engine.get("gaming_mode") or not _engine.get("active_pid"):
        return None
    return {
        "exe":      _engine.get("active_exe"),
        "display":  _engine.get("active_game"),
        "pid":      _engine.get("active_pid"),
        "oc_core":  int(_engine.get("active_oc_core", 0) or 0),
        "oc_mem":   int(_engine.get("active_oc_mem",  0) or 0),
    }


# ─── v3.3 — Multi-game tracking ───────────────────────────────────────
# The engine still tracks ONE "primary" game for game-mode optimization
# (because system tweaks are applied once and shared), but consumers
# like the clipper + screenshotter need to know about ALL running games
# so they can follow whichever has focus.

def get_active_games() -> list:
    """Return every known game process currently running, foreground
    first.  Each entry: {pid, exe, name, ws_mb, score, foreground}.
    Cheap — re-uses the same scanner the engine loop uses."""
    return _scan_game_candidates()


def _foreground_exe_via_user32() -> tuple:
    """Helper: read the foreground window's exe + pid via Win32.
    Returns (pid, exe_name_lower) or (None, '')."""
    try:
        import ctypes
        from ctypes import wintypes
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd: return (None, "")
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value: return (None, "")
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h: return (pid.value, "")
        try:
            buf = (ctypes.c_wchar * 260)()
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return (pid.value, os.path.basename(buf.value).lower())
        finally:
            kernel32.CloseHandle(h)
    except Exception: pass
    return (None, "")


def get_foreground_game() -> dict | None:
    """Return the currently-foregrounded game (if any).  Different from
    get_active_game_info(): this reads the foreground window EVERY time
    rather than returning the engine's frozen primary tracker.  Used
    by the clipper to follow whichever game has focus."""
    fg_pid, fg_exe = _foreground_exe_via_user32()
    if not fg_exe or fg_exe not in KNOWN_GAME_EXES:
        return None
    return {
        "exe":     fg_exe,
        "pid":     fg_pid,
        "name":    _nice_name(fg_exe),
    }
