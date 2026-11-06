#!/usr/bin/env python3
"""Sports scoreboard for Raspberry Pi (pygame).

Detects the attached screen's resolution, scales every UI element against a
1920x1080 reference design, and pages through games if they don't all fit.
Scores come from ESPN's unofficial scoreboard endpoints.
"""
import os
import sys
import threading
import time

import pygame
import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")

REF_W, REF_H = 1920, 1080          # design reference resolution
CARD_W, CARD_H = 600, 210           # card size in reference pixels
GAP, MARGIN = 30, 40
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard"

BG = (14, 17, 23)
CARD = (28, 33, 43)
CARD_LIVE = (36, 44, 58)
TEXT = (235, 238, 243)
DIM = (140, 148, 160)
LIVE = (255, 82, 82)
FINAL = (160, 168, 180)


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------
# Data layer: background thread polls ESPN and caches the last good result.
# --------------------------------------------------------------------------
class ScoreFeed(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.games = []
        self.last_ok = None      # timestamp of last successful fetch
        self.error = None

    def snapshot(self):
        with self.lock:
            return list(self.games), self.last_ok, self.error

    def run(self):
        while True:
            live = False
            try:
                games = self.fetch_all()
                live = any(g["state"] == "in" for g in games)
                with self.lock:
                    self.games = games
                    self.last_ok = time.time()
                    self.error = None
            except Exception as e:  # keep showing cached data on failure
                with self.lock:
                    self.error = str(e)[:80]
            poll = self.cfg.get("poll_live_seconds", 15) if live \
                else self.cfg.get("poll_idle_seconds", 120)
            time.sleep(poll)

    def fetch_all(self):
        favs = {t.upper() for t in self.cfg.get("favorite_teams") or []}
        out = []
        for league in self.cfg["leagues"]:
            r = requests.get(ESPN_URL.format(league=league), timeout=10)
            r.raise_for_status()
            tag = league.split("/")[-1].upper()
            for ev in r.json().get("events", []):
                g = parse_event(ev, tag)
                if not g:
                    continue
                if favs and not ({g["away"]["abbr"], g["home"]["abbr"]} & favs):
                    continue
                out.append(g)
        # live first, then upcoming, then final
        order = {"in": 0, "pre": 1, "post": 2}
        out.sort(key=lambda g: (order.get(g["state"], 3), g["start"]))
        return out


def parse_event(ev, league_tag):
    try:
        comp = ev["competitions"][0]
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        status = ev["status"]["type"]

        def side(c):
            return {
                "abbr": c["team"].get("abbreviation", "?"),
                "name": c["team"].get("shortDisplayName", c["team"].get("name", "?")),
                "score": c.get("score", "0"),
            }

        return {
            "league": league_tag,
            "away": side(teams["away"]),
            "home": side(teams["home"]),
            "state": status.get("state", "pre"),      # pre / in / post
            "detail": status.get("shortDetail", ""),
            "start": ev.get("date", ""),
        }
    except (KeyError, IndexError):
        return None


# --------------------------------------------------------------------------
# Display layer
# --------------------------------------------------------------------------
class Display:
    def __init__(self, cfg):
        self.cfg = cfg
        pygame.init()
        pygame.mouse.set_visible(False)
        if cfg.get("fullscreen", True):
            # (0, 0) = use the screen's native resolution
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
        self.fonts = {}
        self.update_scale()

    def update_scale(self):
        w, h = self.screen.get_size()
        self.w, self.h = w, h
        self.scale = min(w / REF_W, h / REF_H)
        self.fonts.clear()

    def px(self, v):
        return max(1, int(v * self.scale))

    def font(self, ref_size, bold=False):
        key = (ref_size, bold)
        if key not in self.fonts:
            self.fonts[key] = pygame.font.SysFont(
                "dejavusans,liberationsans,arial", self.px(ref_size), bold=bold
            )
        return self.fonts[key]

    def text(self, s, ref_size, color, pos, bold=False, anchor="topleft"):
        surf = self.font(ref_size, bold).render(str(s), True, color)
        rect = surf.get_rect(**{anchor: pos})
        self.screen.blit(surf, rect)
        return rect

    def grid(self):
        """How many cards fit across/down at the current scale."""
        m, gap = self.px(MARGIN), self.px(GAP)
        cw = min(self.px(CARD_W), self.w - 2 * m)
        ch = min(self.px(CARD_H), self.h - 2 * m)
        cols = max(1, (self.w - 2 * m + gap) // (cw + gap))
        rows = max(1, (self.h - 2 * m - self.px(60) + gap) // (ch + gap))
        return cols, rows, cw, ch, m, gap

    def draw(self, games, page, last_ok, error):
        self.screen.fill(BG)
        cols, rows, cw, ch, m, gap = self.grid()
        per_page = cols * rows
        pages = max(1, -(-len(games) // per_page))
        page %= pages
        visible = games[page * per_page:(page + 1) * per_page]

        if not games:
            msg = "Waiting for scores..." if not error else "Can't reach score feed"
            self.text(msg, 60, DIM, (self.w // 2, self.h // 2), anchor="center")

        # center the grid horizontally
        used_w = cols * cw + (cols - 1) * gap
        x0 = (self.w - used_w) // 2
        for i, g in enumerate(visible):
            r, c = divmod(i, cols)
            x = x0 + c * (cw + gap)
            y = m + r * (ch + gap)
            self.draw_card(g, x, y, cw, ch)

        self.draw_footer(page, pages, last_ok, error)
        pygame.display.flip()
        return pages

    def draw_card(self, g, x, y, w, h):
        live = g["state"] == "in"
        pygame.draw.rect(self.screen, CARD_LIVE if live else CARD,
                         (x, y, w, h), border_radius=self.px(18))
        pad = self.px(26)

        # status line
        if live:
            pygame.draw.circle(self.screen, LIVE,
                               (x + pad + self.px(8), y + pad + self.px(14)), self.px(8))
            self.text(g["detail"], 28, LIVE, (x + pad + self.px(26), y + pad), bold=True)
        else:
            col = FINAL if g["state"] == "post" else DIM
            self.text(g["detail"], 28, col, (x + pad, y + pad))
        self.text(g["league"], 24, DIM, (x + w - pad, y + pad), anchor="topright")

        # team rows
        row_y = y + pad + self.px(60)
        for side in ("away", "home"):
            t = g[side]
            self.text(t["name"], 44, TEXT, (x + pad, row_y), bold=True)
            if g["state"] != "pre":
                self.text(t["score"], 52, TEXT, (x + w - pad, row_y - self.px(4)),
                          bold=True, anchor="topright")
            row_y += self.px(62)

    def draw_footer(self, page, pages, last_ok, error):
        y = self.h - self.px(44)
        if pages > 1:
            self.text(f"{page + 1}/{pages}", 26, DIM, (self.w - self.px(40), y),
                      anchor="topright")
        if error:
            self.text("offline - showing last data", 24, LIVE, (self.px(40), y))
        elif last_ok:
            self.text("updated " + time.strftime("%H:%M:%S", time.localtime(last_ok)),
                      24, DIM, (self.px(40), y))


def main():
    cfg = load_config()
    feed = ScoreFeed(cfg)
    feed.start()
    disp = Display(cfg)

    clock = pygame.time.Clock()
    page, last_flip = 0, time.time()
    page_secs = cfg.get("page_seconds", 10)

    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                pygame.quit()
                sys.exit(0)
            if e.type == pygame.VIDEORESIZE:
                disp.update_scale()

        games, last_ok, error = feed.snapshot()
        pages = disp.draw(games, page, last_ok, error)
        if time.time() - last_flip >= page_secs:
            page = (page + 1) % pages
            last_flip = time.time()
        clock.tick(10)   # a scoreboard doesn't need more than 10 fps


if __name__ == "__main__":
    main()
