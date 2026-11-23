"""Build a tiny RPG Maker MV game from the MIT corescript and generated data/images.

usage: python3 -I make_game.py <corescript src dir> <out dir> <ttf font>
"""

import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

src, out, font = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
if out.exists():
    shutil.rmtree(out)
(out / "js" / "libs").mkdir(parents=True)

# engine: concatenate like concat.js does
for name in ("rpg_core", "rpg_managers", "rpg_objects", "rpg_scenes", "rpg_sprites", "rpg_windows"):
    parts = json.loads((src / f"{name}.json").read_text())
    (out / "js" / f"{name}.js").write_text("\n".join((src / p).read_text() for p in parts))
for lib in (src / "js" / "libs").iterdir():
    shutil.copy(lib, out / "js" / "libs" / lib.name)
shutil.copy(src / "js" / "main.js", out / "js" / "main.js")
(out / "js" / "plugins.js").write_text("var $plugins = [];\n")
shutil.copy(src / "template" / "index.html", out / "index.html")
(out / "fonts").mkdir()
shutil.copy(src / "template" / "fonts" / "gamefont.css", out / "fonts" / "gamefont.css")
shutil.copy(font, out / "fonts" / "mplus-1m-regular.ttf")


def img(rel, size, color, grid=None):
    p = out / "img" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", size, color)
    if grid:
        d = ImageDraw.Draw(im)
        for x in range(0, size[0], grid):
            for y in range(0, size[1], grid):
                if (x // grid + y // grid) % 2:
                    d.rectangle([x, y, x + grid - 1, y + grid - 1], fill=(255, 255, 255, 255))
    im.save(p)


win = Image.new("RGBA", (192, 192), (20, 40, 120, 230))
d = ImageDraw.Draw(win)
for i in range(32):  # text colours: 12x12 cells from (96,144)
    x, y = 96 + (i % 8) * 12, 144 + (i // 8) * 12
    d.rectangle([x, y, x + 11, y + 11], fill=(255, 255 - i * 6, 255, 255))
(out / "img" / "system").mkdir(parents=True)
win.save(out / "img" / "system" / "Window.png")
for n in (
    "IconSet",
    "Balloon",
    "Shadow1",
    "Shadow2",
    "Damage",
    "States",
    "Weapons1",
    "Weapons2",
    "Weapons3",
    "ButtonSet",
):
    img(f"system/{n}.png", (96, 96), (0, 0, 0, 0))
img("titles1/Title.png", (816, 624), (40, 90, 60, 255), grid=48)
img("characters/Actor1.png", (576, 384), (200, 60, 60, 255), grid=24)
img("tilesets/B.png", (768, 768), (90, 70, 40, 255), grid=48)
img("faces/.keep.png", (1, 1), (0, 0, 0, 0))

audio = {"name": "", "pan": 0, "pitch": 100, "volume": 90}
vehicle = {"bgm": audio, "characterIndex": 0, "characterName": "", "startMapId": 0, "startX": 0, "startY": 0}
system = {
    "airship": vehicle,
    "boat": vehicle,
    "ship": vehicle,
    "armorTypes": ["", "General"],
    "attackMotions": [{"type": 0, "weaponImageId": 0}] * 13,
    "battleBgm": audio,
    "battleback1Name": "",
    "battleback2Name": "",
    "battlerHue": 0,
    "battlerName": "",
    "currencyUnit": "G",
    "defeatMe": audio,
    "editMapId": 1,
    "elements": ["", "Physical"],
    "equipTypes": ["", "Weapon", "Shield", "Head", "Body", "Accessory"],
    "gameTitle": "Fixture Quest",
    "gameoverMe": audio,
    "locale": "en_US",
    "magicSkills": [1],
    "menuCommands": [True] * 6,
    "optDisplayTp": True,
    "optDrawTitle": True,
    "optExtraExp": False,
    "optFloorDeath": False,
    "optFollowers": True,
    "optSideView": False,
    "optSlipDeath": False,
    "optTransparent": False,
    "partyMembers": [1],
    "skillTypes": ["", "Magic"],
    "sounds": [audio] * 24,
    "startMapId": 1,
    "startX": 8,
    "startY": 6,
    "switches": ["", ""],
    "testBattlers": [],
    "testTroopId": 0,
    "title1Name": "Title",
    "title2Name": "",
    "titleBgm": audio,
    "variables": ["", ""],
    "versionId": 1,
    "victoryMe": audio,
    "weaponTypes": ["", "Sword"],
    "windowTone": [0, 0, 0, 0],
    "terms": {
        "basic": ["Level", "Lv", "HP", "HP", "MP", "MP", "TP", "TP", "EXP", "EXP"],
        "commands": [
            "Fight",
            "Escape",
            "Attack",
            "Guard",
            "Item",
            "Skill",
            "Equip",
            "Status",
            "Formation",
            "Save",
            "Game End",
            "Options",
            "Weapon",
            "Armor",
            "Key Item",
            "Equip",
            "Optimize",
            "Clear",
            "New Game",
            "Continue",
            None,
            "To Title",
            "Cancel",
            None,
            "Buy",
            "Sell",
        ],
        "params": [
            "Max HP",
            "Max MP",
            "Attack",
            "Defense",
            "M.Attack",
            "M.Defense",
            "Agility",
            "Luck",
            "Hit",
            "Evasion",
        ],
        "messages": {
            "saveMessage": "Save to which file?",
            "loadMessage": "Load which file?",
            "file": "File",
            "partyName": "%1's Party",
            "possession": "Possession",
            "expTotal": "Current %1",
            "expNext": "To Next %1",
            "alwaysDash": "Always Dash",
            "commandRemember": "Command Remember",
            "bgmVolume": "BGM Volume",
            "bgsVolume": "BGS Volume",
            "meVolume": "ME Volume",
            "seVolume": "SE Volume",
        },
    },
}
w, h = 17, 13
tiles = [0] * (w * h * 6)
for i in range(w * h):  # layer 2 (index 2): B tiles in a pattern
    if i % 3 == 0:
        tiles[2 * w * h + i] = 1 + i % 8
damage = {"type": 0, "elementId": 0, "formula": "0", "variance": 0, "critical": False}
skill = {
    "iconIndex": 0,
    "description": "",
    "stypeId": 0,
    "scope": 1,
    "occasion": 1,
    "speed": 0,
    "successRate": 100,
    "repeats": 1,
    "tpGain": 0,
    "hitType": 1,
    "animationId": 0,
    "damage": damage,
    "effects": [],
    "mpCost": 0,
    "tpCost": 0,
    "message1": "",
    "message2": "",
    "requiredWtypeId1": 0,
    "requiredWtypeId2": 0,
    "note": "",
}
data = {
    "Actors": [
        None,
        {
            "id": 1,
            "name": "Hero",
            "nickname": "",
            "profile": "",
            "classId": 1,
            "initialLevel": 1,
            "maxLevel": 99,
            "characterName": "Actor1",
            "characterIndex": 0,
            "faceName": "",
            "faceIndex": 0,
            "battlerName": "",
            "equips": [0, 0, 0, 0, 0],
            "traits": [],
            "note": "",
        },
    ],
    "Classes": [
        None,
        {
            "id": 1,
            "name": "Fighter",
            "expParams": [30, 20, 30, 30],
            "params": [[100 + lv for lv in range(100)] for _ in range(8)],
            "learnings": [],
            "traits": [],
            "note": "",
        },
    ],
    "Skills": [None, {"id": 1, "name": "Attack", **skill}, {"id": 2, "name": "Guard", **skill}],
    "Items": [None],
    "Weapons": [None],
    "Armors": [None],
    "Enemies": [None],
    "Troops": [None],
    "Animations": [None],
    "CommonEvents": [None],
    "States": [
        None,
        {
            "id": 1,
            "name": "Knockout",
            "restriction": 4,
            "priority": 100,
            "motion": 3,
            "overlay": 0,
            "removeAtBattleEnd": False,
            "removeByRestriction": False,
            "autoRemovalTiming": 0,
            "minTurns": 1,
            "maxTurns": 1,
            "removeByDamage": False,
            "chanceByDamage": 100,
            "removeByWalking": False,
            "stepsToRemove": 100,
            "traits": [],
            "iconIndex": 0,
            "message1": "",
            "message2": "",
            "message3": "",
            "message4": "",
            "note": "",
        },
    ],
    "Tilesets": [
        None,
        {
            "id": 1,
            "name": "T",
            "mode": 1,
            "tilesetNames": ["", "", "", "", "", "B", "", "", ""],
            "flags": [0] * 8192,
            "note": "",
        },
    ],
    "MapInfos": [
        None,
        {"id": 1, "name": "MAP001", "parentId": 0, "order": 1, "expanded": False, "scrollX": 0, "scrollY": 0},
    ],
    "System": system,
    "Map001": {
        "autoplayBgm": False,
        "autoplayBgs": False,
        "battleback1Name": "",
        "battleback2Name": "",
        "bgm": audio,
        "bgs": audio,
        "disableDashing": False,
        "displayName": "",
        "encounterList": [],
        "encounterStep": 30,
        "height": h,
        "note": "",
        "parallaxLoopX": False,
        "parallaxLoopY": False,
        "parallaxName": "",
        "parallaxShow": True,
        "parallaxSx": 0,
        "parallaxSy": 0,
        "scrollType": 0,
        "specifyBattleback": False,
        "tilesetId": 1,
        "width": w,
        "data": tiles,
        "events": [None],
    },
}
(out / "data").mkdir()
for name, value in data.items():
    (out / "data" / f"{name}.json").write_text(json.dumps(value))
(out / "package.json").write_text(json.dumps({"name": "fixture", "main": "index.html"}))
print("built", out)
