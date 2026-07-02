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
import time
import threading
from config import APPDATA_DIR
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
    "msmpeng.exe","ghostshell.exe","vispora.exe","app.py","flask.exe",
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
}


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
    r = run_cmd(["powercfg","/query","SCHEME_CURRENT","SUB_PROCESSOR","IdleDisable"])
    baseline["cpu_idle_disabled"] = "0x00000001" in r.get("out","")

    return baseline


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
def _apply_gaming_tweaks(pid: int, exe_name: str) -> list[str]:
    """Apply hard gaming tweaks."""
    changes = []

    # 1. Process priority -> High
    r = run_ps(f"try {{ (Get-Process -Id {pid}).PriorityClass='High'; 'OK' }} catch {{ 'FAIL' }}", timeout=5)
    if not (r["ok"] and "OK" in r["out"]):
        run_cmd(["wmic","process","where",f"ProcessId={pid}","CALL","setpriority","128"])
    changes.append("Process priority -> High")

    # 2. CPU affinity to best cores
    mask = _get_best_affinity()
    if mask and mask != "0":
        run_ps(f"try {{ (Get-Process -Id {pid}).ProcessorAffinity=[IntPtr]{mask} }} catch {{}}", timeout=5)
        changes.append(f"CPU affinity -> 0x{int(mask):x}")

    # 3. Win32PrioritySeparation -> 0x28 (Short/Fixed)
    run_cmd(["reg","add",r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
             "/v","Win32PrioritySeparation","/t","REG_DWORD","/d","40","/f"])
    changes.append("Win32PrioritySeparation -> 0x28 (Short/Fixed)")

    # 4. CPU idle disabled (max clocks)
    run_cmd(["powercfg","/setacvalueindex","SCHEME_CURRENT","SUB_PROCESSOR","IdleDisable","1"])
    run_cmd(["powercfg","/setactive","SCHEME_CURRENT"])
    changes.append("CPU idle -> disabled")

    # 5. SystemResponsiveness -> 10
    # beta.6: was 0.  Setting 0 dedicates 100% of the multimedia class to
    # foreground threads and STARVES Audiosrv (Discord / in-game voice
    # crackle under load).  The rest of the app (optimizer, network_tweaks)
    # standardized on Microsoft's gaming value 10, and auto_recover actively
    # reverts any 0 it finds — so a per-game 0 here just got undone.  Aligned.
    run_cmd(["reg","add",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
             "/v","SystemResponsiveness","/t","REG_DWORD","/d","10","/f"])
    changes.append("SystemResponsiveness -> 10")

    # 6. NetworkThrottlingIndex -> max
    run_cmd(["reg","add",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
             "/v","NetworkThrottlingIndex","/t","REG_DWORD","/d","4294967295","/f"])
    changes.append("Network throttling -> disabled")

    # 7. DSCP 46 (highest priority — EF / expedited forwarding) for the game.
    #    exe_name comes from live process enumeration, so validate it as a
    #    plain .exe basename before it touches a PowerShell -Command string —
    #    a process/folder named with a quote or semicolon would otherwise
    #    inject into our elevated shell.  Reject → just skip the QoS tag.
    if is_safe_ps_exe_name(exe_name):
        rule = f"Vispora_Game_{exe_name.replace('.exe','').replace(' ','_')}"
        run_ps(f'Remove-NetQosPolicy -Name "{rule}" -Confirm:$false -ErrorAction SilentlyContinue')
        run_ps(f'New-NetQosPolicy -Name "{rule}" -AppPathNameMatchCondition "{exe_name}" '
               f'-DSCPAction 46 -NetworkProfile All -Confirm:$false -ErrorAction SilentlyContinue')
        changes.append(f"DSCP 46 for {exe_name}")
    else:
        log.warning(f"  skipped DSCP QoS for unsafe exe name: {exe_name!r}")

    # 8. Streaming software — prioritize any running streaming apps
    #    (DSCP 34 = AF41, Assured Forwarding high — below the game but above default)
    streamers = _find_running_streaming_exes()
    for s in streamers:
        s_exe = s["exe"]
        s_pid = s["pid"]
        # Raise streamer process priority to AboveNormal (not High — game gets High)
        run_ps(f"try {{ (Get-Process -Id {s_pid}).PriorityClass='AboveNormal' }} catch {{}}", timeout=3)
        # DSCP 34 for streamer traffic — same exe-name validation as the game.
        if not is_safe_ps_exe_name(s_exe):
            log.warning(f"  skipped stream QoS for unsafe exe name: {s_exe!r}")
            continue
        s_rule = f"Vispora_Stream_{s_exe.replace('.exe','').replace(' ','_')}"
        run_ps(f'Remove-NetQosPolicy -Name "{s_rule}" -Confirm:$false -ErrorAction SilentlyContinue')
        run_ps(f'New-NetQosPolicy -Name "{s_rule}" -AppPathNameMatchCondition "{s_exe}" '
               f'-DSCPAction 34 -NetworkProfile All -Confirm:$false -ErrorAction SilentlyContinue')
        changes.append(f"Stream priority: {s_exe}")

    _engine["streaming_exes"] = [s["exe"] for s in streamers]
    if streamers:
        log.info(f"  Prioritized {len(streamers)} streaming app(s): {', '.join(s['exe'] for s in streamers)}")

    _set_gaming_flag()

    # v3.4 — bump GhostShell out of efficiency mode so the mapper /
    # HidHide retry / network monitor / etc. stay responsive while the
    # game runs at HIGH priority.  Without this, GhostShell sits at
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

    # 9. Background pauser — freeze AI apps + browsers (opt-in)
    try:
        from core import background_pauser
        if background_pauser.get_settings().get("enabled"):
            r = background_pauser.pause_now(reason=f"game start: {exe_name}")
            if r.get("ok") and r.get("paused"):
                changes.append(f"Paused {len(r['paused'])} bg apps "
                               f"(~{r['total_freed_mb']} MB)")
                log.info(f"  Paused {len(r['paused'])} background apps, "
                         f"freed ~{r['total_freed_mb']} MB")
    except Exception as e:
        log.debug(f"background pauser hook failed: {e}")

    log.info(f"  Applied {len(changes)} gaming tweaks")
    return changes


# ── Per-game optimization target dispatch (beta.13) ──────────────────
# Holds what apply_target() changed for the CURRENTLY-tracked game, so
# restore_target() can reverse exactly that on exit.  The engine tracks
# one game at a time, so a single slot is sufficient.
_target_restore: dict = {}


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

      max_fps     → hold the user's PROVEN saved OC at 100% GPU power +
                    Ultimate power plan.  AT is off for the game (set with
                    the target) so nothing caps the card back down.
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
    if not r:
        return
    target = r.get("target")
    try:
        if target == "max_fps":
            from core import gpu_overclock
            oc = r.get("oc") or {}
            # Re-apply the saved profile (resets GPU power_pct to the user's
            # normal value) then restore the prior power plan.
            gpu_overclock.apply_oc(core_offset_mhz=int(oc.get("core", 0)),
                                   mem_offset_mhz=int(oc.get("mem", 0)),
                                   power_pct=int(oc.get("power", 100)))
            guid = r.get("power_guid") or ""
            if guid:
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
    log.info(f"Restoring normal mode (reason: {reason})...")
    # beta.13 — reverse any per-game optimization-target posture first
    # (GPU OC / power plan / competitive latency), before the generic tweaks.
    restore_target()
    # Capture the game name BEFORE we clear engine state, so we can use it in the notification.
    _prev_game = _engine.get("active_game")
    baseline = _load_baseline()

    # If we have no baseline, use sensible Windows defaults
    priority_sep = baseline.get("priority_sep", "2")  # 0x02 = Windows default
    sys_resp = baseline.get("sys_responsiveness", "14")  # 0x14 = 20 decimal = default
    net_throttle = baseline.get("net_throttle", "a")  # 0x0a = 10 = default
    cpu_idle_was_disabled = baseline.get("cpu_idle_disabled", False)

    # 1. Restore Win32PrioritySeparation
    val = int(priority_sep, 16)
    run_cmd(["reg","add",r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
             "/v","Win32PrioritySeparation","/t","REG_DWORD","/d",str(val),"/f"])
    log.info(f"  Restored Win32PrioritySeparation -> 0x{priority_sep}")

    # 2. Restore SystemResponsiveness
    val2 = int(sys_resp, 16)
    run_cmd(["reg","add",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
             "/v","SystemResponsiveness","/t","REG_DWORD","/d",str(val2),"/f"])
    log.info(f"  Restored SystemResponsiveness -> {val2}")

    # 3. Restore NetworkThrottlingIndex
    val3 = int(net_throttle, 16)
    run_cmd(["reg","add",r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
             "/v","NetworkThrottlingIndex","/t","REG_DWORD","/d",str(val3),"/f"])
    log.info(f"  Restored NetworkThrottlingIndex -> {val3}")

    # 4. Restore CPU idle
    if not cpu_idle_was_disabled:
        run_cmd(["powercfg","/setacvalueindex","SCHEME_CURRENT","SUB_PROCESSOR","IdleDisable","0"])
        run_cmd(["powercfg","/setactive","SCHEME_CURRENT"])
        log.info("  Restored CPU idle -> enabled")

    # 5. Remove any DSCP game rules — both the current Vispora_ prefix and
    #    the legacy GhostShell_ prefix so rules created before the rename
    #    still get cleaned up.
    run_ps('Get-NetQosPolicy | Where-Object {$_.Name -like "Vispora_Game_*" -or $_.Name -like "GhostShell_Game_*"} | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue')
    log.info("  Removed DSCP game rules")

    # 5b. Remove streaming DSCP rules & restore streamer process priority to Normal
    run_ps('Get-NetQosPolicy | Where-Object {$_.Name -like "Vispora_Stream_*" -or $_.Name -like "GhostShell_Stream_*"} | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue')
    streamers = _engine.get("streaming_exes", []) or []
    for s_exe in streamers:
        # Revert any still-running streamer process to Normal priority
        base = s_exe.replace(".exe", "")
        run_ps(f"try {{ Get-Process -Name '{base}' -ErrorAction SilentlyContinue | ForEach-Object {{ $_.PriorityClass='Normal' }} }} catch {{}}", timeout=3)
    _engine["streaming_exes"] = []
    if streamers:
        log.info(f"  Reverted {len(streamers)} streaming app(s) to normal priority")

    # v3 — capture exe + active OC BEFORE we wipe engine state, so the
    # crash-recovery handler has the data it needs.
    _prev_exe       = _engine.get("active_exe")
    _prev_oc_core   = int(_engine.get("active_oc_core", 0) or 0)
    _prev_oc_mem    = int(_engine.get("active_oc_mem", 0) or 0)

    _clear_gaming_flag()
    _engine["gaming_mode"] = False
    _engine["changes"] = []
    _engine["active_game"] = None
    _engine["active_exe"] = None
    _engine["active_pid"] = None
    _engine["active_oc_core"] = 0
    _engine["active_oc_mem"]  = 0
    log.info("  Normal mode restored")

    # v3.4 — restore GhostShell efficiency mode now that the game is
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
        if bp_settings.get("enabled") and bp_settings.get("auto_resume_on_game_exit", True):
            r = background_pauser.resume_now(reason=f"restore_normal: {reason}")
            if r.get("resumed"):
                log.info(f"  Resumed {len(r['resumed'])} background apps")
    except Exception as e:
        log.debug(f"background pauser resume hook failed: {e}")

    # 6. Post-game refresh — clean up RAM/caches/orphan processes so the PC is
    #    fresh for the next game session. Done in a background thread so the
    #    notification fires immediately and we don't block.
    if reason in ("game exit",):
        threading.Thread(target=_post_game_refresh, args=(_prev_game,), daemon=True).start()

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


def _post_game_refresh(game_name: str = None):
    """Refresh the PC state after a game exits — frees RAM, kills orphan processes,
    flushes caches, so the next game launches into a clean state.

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

    # 3. Kill orphaned game-related processes that often linger after game exit
    #    (overlay components, anti-cheat helpers, shader compilers, etc.)
    orphan_targets = [
        "EpicWebHelper.exe", "EpicGamesLauncher.exe.tmp",
        "BattlEye-Service.exe.bak", "EasyAntiCheat_Setup.exe",
        "ShaderCompileWorker-Win64-Shipping.exe", "CrashReportClient.exe",
        "UnrealCEFSubProcess.exe", "Steamwebhelper.exe.tmp",
        "DiscordOverlay.exe", "ScreenshotUploader.exe",
        "vrserver.exe", "vrcompositor.exe",  # SteamVR leftovers
    ]
    killed = 0
    for proc in orphan_targets:
        base = proc.replace(".exe", "").replace(".tmp", "").replace(".bak", "")
        r = run_ps(
            f"try {{ Get-Process -Name '{base}' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; if ($?) {{ 'killed' }} }} catch {{}}",
            timeout=4,
        )
        if r.get("ok") and "killed" in (r.get("out") or ""):
            killed += 1
    if killed > 0:
        log.info(f"  Killed {killed} orphaned game-related process(es)")

    # 4. Reset MMCSS gaming task scheduler state — bounces the multimedia
    #    scheduler so the next game gets a clean priority slot.
    try:
        run_ps("Restart-Service -Name 'MMCSS' -Force -ErrorAction SilentlyContinue", timeout=8)
        log.info("  MMCSS scheduler refreshed")
    except Exception:
        pass

    # 5. Clear shader pipeline cache lock files (DXIL/SPIR-V locks left behind
    #    by crashed shader compilers — keeping them stale slows next launch).
    try:
        run_ps(
            r"""
$paths = @(
    "$env:LOCALAPPDATA\NVIDIA\DXCache",
    "$env:LOCALAPPDATA\NVIDIA\GLCache",
    "$env:LOCALAPPDATA\AMD\DxCache",
    "$env:LOCALAPPDATA\D3DSCache"
)
foreach ($p in $paths) {
    if (Test-Path $p) {
        Get-ChildItem -Path $p -Filter "*.lock" -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
        Get-ChildItem -Path $p -Filter "*.tmp" -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}
""",
            timeout=8,
        )
        log.info("  Shader cache lock/tmp files cleared")
    except Exception:
        pass

    # 6. Trigger Windows page combine to recompact RAM after working set drop
    try:
        run_ps("[System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers(); [System.GC]::Collect()", timeout=5)
    except Exception:
        pass

    # 7. Re-arm the boot prep cache so subsequent game launches pre-cache hot data
    try:
        run_ps("Get-Process -Name 'rundll32' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq '' -and $_.StartTime -gt (Get-Date).AddMinutes(-10) } | Stop-Process -Force -ErrorAction SilentlyContinue", timeout=5)
    except Exception:
        pass

    duration = time.time() - started
    log.info(f"=== Post-game refresh done in {duration:.1f}s ===")
    if _notifier and freed_total > 0:
        try:
            mb = freed_total / (1024 * 1024)
            _notifier.toast(
                "GhostShell — PC Refreshed",
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
    r2 = run_cmd(["powercfg","/query","SCHEME_CURRENT","SUB_PROCESSOR","IdleDisable"])
    idle_disabled = "0x00000001" in r2.get("out","")
    baseline_idle = baseline.get("cpu_idle_disabled", False)
    if idle_disabled and not baseline_idle:
        log.info("CPU idle is disabled but baseline says enabled — restoring")
        restore_normal_mode("stale CPU idle tweak detected")


def recapture_baseline():
    """Manually recapture the baseline (e.g. after user applies permanent tweaks they want to keep)."""
    log.info("Recapturing system baseline...")
    baseline = _capture_baseline()
    _save_baseline(baseline)
    log.info(f"  New baseline: {baseline}")
    return {"ok": True, "baseline": baseline}


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════
def _get_best_affinity() -> str:
    r = run_ps("""
$cpu = Get-CimInstance Win32_Processor; $name = $cpu.Name; $l = $cpu.NumberOfLogicalProcessors; $p = $cpu.NumberOfCores
$mask = 0
if ($name -match '12th|13th|14th|Core Ultra|Raptor|Alder') {
    $t = [Math]::Min($l, $p); for ($i=0; $i -lt $t; $i++) { $mask = $mask -bor (1 -shl $i) }
} else { for ($i=0; $i -lt $l; $i++) { $mask = $mask -bor (1 -shl $i) } }
$mask
""", timeout=5)
    if r["ok"] and r["out"]:
        try: return str(int(r["out"].strip()))
        except ValueError: pass
    return "0"


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
    try:
        r = run_ps(
            "Get-Process | Select-Object Id,ProcessName | ConvertTo-Json", timeout=8)
        if not (r["ok"] and r["out"]):
            return []
        procs = json.loads(r["out"])
        if not isinstance(procs, list):
            procs = [procs]
        found = []
        for p in procs:
            pname = (p.get("ProcessName","") or "").lower()
            pid = p.get("Id", 0)
            exe = pname + ".exe"
            if exe in STREAMING_EXES and pid:
                found.append({"exe": exe, "pid": pid})
        return found
    except Exception:
        return []


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


def _monitor_loop():
    log.info("Game monitor started")

    while _engine["monitoring"]:
        try:
            if _engine["gaming_mode"] and _engine["active_pid"]:
                # Check if the tracked game is still alive.
                pid = _engine["active_pid"]
                r = run_ps(f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Id", timeout=5)
                alive = r["ok"] and r["out"].strip().isdigit()
                if not alive:
                    game = _engine["active_game"] or "Unknown"
                    log.info(f"Tracked game exited: {game} (PID {pid})")
                    # v3.3 — Multi-game support: if ANY other known game is
                    # still running, don't restore normal mode — just switch
                    # tracking to it.  Game-mode tweaks are system-wide so
                    # the existing optimizations stay relevant; we just
                    # update the "which game is the user playing" pointer.
                    others = _scan_game_candidates()
                    if others:
                        new = others[0]
                        log.info(
                            f"  Other known game(s) still running: switching "
                            f"tracker to {new['name']} (PID {new['pid']})"
                        )
                        _engine["active_pid"]  = new["pid"]
                        _engine["active_exe"]  = new["exe"]
                        _engine["active_game"] = new["name"]
                        # Notify AT so it can switch its target too
                        try:
                            from core import adaptive_tuning as _at
                            _at.on_game_exit(_engine.get("active_exe", ""),
                                             game, was_crash=False,
                                             reason="game-mode tracker switch")
                            _at.on_game_start(new["exe"], new["name"])
                        except Exception as e:
                            log.debug(f"AT switch failed: {e}")
                    else:
                        restore_normal_mode("game exit")
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
                    best = candidates[0]
                    pid, exe, display = best["pid"], best["exe"], best["name"]
                    fg = " [foreground]" if best["foreground"] else ""
                    log.info(
                        f"Game detected: {display} ({exe}, PID {pid}, "
                        f"{best['ws_mb']} MB{fg}, score {best['score']})"
                    )
                    # Capture baseline right before applying (freshest snapshot)
                    if not _engine["gaming_mode"]:
                        baseline = _capture_baseline()
                        _save_baseline(baseline)
                    # Fire the notification BEFORE the tweaks — user sees it immediately.
                    if _notifier:
                        try:
                            _notifier.notify_game_detected(display, exe)
                        except Exception as e:
                            log.debug(f"Notifier failed: {e}")
                    changes = _apply_gaming_tweaks(pid, exe)
                    _engine["active_game"] = display
                    _engine["active_exe"] = exe
                    _engine["active_pid"] = pid
                    _engine["gaming_mode"] = True
                    _engine["changes"] = changes
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
        except Exception as e:
            log.warning(f"Monitor error: {e}")
        time.sleep(3)

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
    endpoint so OTHER GhostShell users get this exe in their known
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


def set_settings(auto_enable_on_boot=None) -> dict:
    s = _load_gp_settings()
    if auto_enable_on_boot is not None:
        s["auto_enable_on_boot"] = bool(auto_enable_on_boot)
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
    t = threading.Thread(target=_monitor_loop, daemon=True)
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
