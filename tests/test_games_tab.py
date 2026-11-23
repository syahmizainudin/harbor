"""The Games tab: rpgm-shelf mounted at /games and /api/games/, its permissions, play tokens for
harbor's users, audit lines, and a planted session cookie that can't hide the real one."""

import http.client
import json
import unittest
from pathlib import Path

from test_permissions import PW, HttpBase

from harbor import accounts as A


def make_game(folder: Path, title: str) -> None:
    """A fake MV game: what rpgm-shelf's scanner looks at."""
    (folder / "js").mkdir(parents=True)
    (folder / "data").mkdir()
    (folder / "index.html").write_text('<html><script src="js/main.js"></script></html>')
    (folder / "js" / "rpg_core.js").write_text("//")
    (folder / "js" / "main.js").write_text("//")
    (folder / "data" / "System.json").write_text(json.dumps({"gameTitle": title}))


class GamesTabTests(HttpBase):
    GAMES = True

    def setUp(self):
        super().setUp()
        make_game(self.dir / "Games" / "Quest", "Quest")

    def guest(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = conn.getresponse()
        out = r.status, r.getheader("Location"), r.read()
        conn.close()
        return out

    def add_folder(self):
        status, res = self.req("Ann", "POST", "/api/games/add-root", {"path": str(self.dir / "Games")})
        self.assertEqual(status, 200, res)
        return res

    def test_admin_adds_a_folder_and_members_play(self):
        root = self.add_folder()
        self.assertEqual(root["games"], 1)
        log = self.acc.audit_log(10, 0, "games folder added")["items"]
        self.assertEqual((log[0]["target"], log[0]["detail"]), (root["path"], "1 game(s)"))
        mo = self.req("Mo", "GET", "/api/state")[1]
        self.assertEqual([g["title"] for g in mo["games"]], ["Quest"])
        self.assertEqual(mo["games_can"], {"play": True, "manage": False})
        self.assertNotIn("folder", mo["games"][0])
        status, play = self.req("Mo", "POST", "/api/games/play", {"id": mo["games"][0]["id"]})
        self.assertEqual(status, 200, play)
        server = self.games[1]
        self.assertEqual(server.tokens.check(play["token"]), (self.acc.get("Mo")["id"], mo["games"][0]["id"]))
        self.assertEqual(play["port"], server.port)
        self.assertEqual(self.req("Mo", "POST", "/api/games/add-root", {"path": "/tmp"})[0], 403)
        self.assertEqual(self.req("Ann", "POST", "/api/games/remove-root", {"id": root["id"]})[0], 200)
        log = self.acc.audit_log(10, 0, "games folder removed")["items"]
        self.assertEqual((log[0]["target"], log[0]["detail"]), (root["path"], "saves kept"))

    def test_reader_and_guest_cant_play(self):
        self.add_folder()
        rea = self.req("Rea", "GET", "/api/state")[1]
        self.assertEqual((rea["games"], rea["games_can"]["play"]), ([], False))
        gid = self.games[0].games()[0]["id"]
        self.assertEqual(self.req("Rea", "POST", "/api/games/play", {"id": gid})[0], 403)
        status, _, body = self.guest("GET", "/api/state")
        self.assertEqual(json.loads(body)["games"], [])
        self.assertEqual(self.guest("POST", "/api/games/play", {"id": gid})[0], 401)
        status, location, _ = self.guest("GET", "/games")
        self.assertEqual((status, location), (303, "/login"))
        self.assertEqual(self.req("Mo", "GET", "/games")[0], 200)

    def test_new_and_upgraded_roles(self):
        roles = {r["name"]: r["perms"] for r in self.acc.roles()}
        self.assertIn("games.play", roles["Member"])
        self.assertNotIn("games.play", roles["Reader"])
        self.assertNotIn("games.manage", roles["Member"])


class PlantedCookieTests(HttpBase):
    """A game on another port of the same host can set a cookie named like harbor's session."""

    def state_with(self, cookie):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/state", headers={"Cookie": cookie})
        st = json.loads(conn.getresponse().read())
        conn.close()
        return st["account"]

    def test_the_real_session_wins(self):
        _, sid = self.acc.login("Ann", PW)
        for cookie in (
            f"harbor_session=planted; harbor_session={sid}",
            f"harbor_session={sid}; harbor_session=planted",
            f'other=1; harbor_session="planted"; harbor_session={sid}',
        ):
            self.assertEqual((self.state_with(cookie) or {}).get("name"), "Ann", cookie)
        self.assertIsNone(self.state_with("harbor_session=planted"))
        self.assertEqual(
            A.session_ids({"Cookie": "; ".join(f"harbor_session=x{i}" for i in range(9))}),
            [f"x{i}" for i in range(A.SESSION_COOKIES_MAX)],
        )


class GamesSettingsTests(unittest.TestCase):
    def test_defaults(self):
        from harbor.config import Settings

        s = Settings()
        self.assertEqual((s.games, s.games_port, s.games_db, s.games_scan), (True, 8768, "", True))


if __name__ == "__main__":
    unittest.main()
