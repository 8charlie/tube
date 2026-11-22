"""tube: a rofi front-end for YouTube that plays everything in mpv.

The subscription feed comes from YouTube's per-channel RSS feeds (no API key,
no Invidious/Piped), search and channel pages come from yt-dlp, and playback
is mpv, which itself goes through yt-dlp.

    tube                    open the picker on the subscription feed
    tube search [QUERY]     open the picker on a YouTube search
    tube refresh            refresh the feed and thumbnails (run by a timer)
    tube import-freetube    import subscriptions, history and watch later
"""

import argparse
import concurrent.futures as cf
import fcntl
import functools
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "tube"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "tube"
THUMBS = CACHE / "thumbs"
SUBS = DATA / "subscriptions.json"
WATCHED = DATA / "watched.json"
LATER = DATA / "later.json"
PROGRESS = DATA / "progress"    # one "<position> <duration> <ended>" file per video
FEED = CACHE / "feed.json"
THEME = Path(os.environ.get("TUBE_THEME") or Path(__file__).with_name("tube.rasi"))
PROGRESS_SCRIPT = Path(__file__).with_name("tube_progress.lua")
# mpv's save-position-on-quit files (the default watch-later-dir)
MPV_WATCH_LATER = (Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state")
                   / "mpv/watch_later")

FEED_SIZE = 250          # newest videos kept in the merged feed
STALE = 10 * 60          # feed older than this is refreshed in the background
THUMB_MAX_AGE = 30 * 86400
SEARCH_SIZE = 40
CHANNEL_SIZE = 60
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# --- keys --------------------------------------------------------------------
# Vim style. Normal mode has no text box: plain letters are actions and
# j/k etc. move. i (or /) switches to insert mode, whose text box filters the
# list; Esc or Enter goes back to normal mode keeping the filter, and Enter
# with nothing matching (or Ctrl+Enter) searches YouTube instead.

MOVE_KEYS = {
    "kb-row-down": ["Down", "Control+n", "Control+j"],
    "kb-row-up": ["Up", "Control+p", "Control+k"],
}
NORMAL_BINDINGS = {
    "kb-row-down": MOVE_KEYS["kb-row-down"] + ["j"],
    "kb-row-up": MOVE_KEYS["kb-row-up"] + ["k"],
    "kb-row-first": ["Home", "KP_Home", "g"],
    "kb-row-last": ["End", "KP_End", "Shift+g"],
    "kb-page-next": ["Page_Down", "Control+d"],
    "kb-page-prev": ["Page_Up", "Control+u"],
    "kb-accept-entry": ["Return", "KP_Enter", "l"],
}
# rofi key -> action; each gets a kb-custom-N slot. Capitals must be written
# Shift+<letter>: rofi doesn't match "W".
NORMAL_ACTIONS = {
    "i": "insert",
    "slash": "insert",
    "w": "later",
    "m": "watched",
    "c": "channel",
    "s": "subscribe",
    "y": "copy",
    "Shift+w": "view-later",
    "Shift+s": "view-subs",
    "r": "refresh",
    "h": "back",
    "q": "quit",
}
INSERT_BINDINGS = {
    **MOVE_KEYS,
    # Ctrl+h/l move the cursor like arrow keys
    "kb-move-char-back": ["Left", "Control+b", "Control+h"],
    "kb-move-char-forward": ["Right", "Control+f", "Control+l"],
    "kb-accept-entry": ["Return", "KP_Enter", "Control+m"],
}
INSERT_ACTIONS = {
    "Escape": "normal",
    "Control+Return": "search",
}

# Colours for the key hints, which are Pango markup and can't read the
# theme's variables: surface0, text and mauve from tube.rasi.
CHIP_BG = "#313244"
CHIP_FG = "#cdd6f4"
LABEL_FG = "#cba6f7"


# --- storage -----------------------------------------------------------------

def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def is_finished(pos, dur, ended):
    """Counts as watched once it played to the end or stopped in the last
    5% (at least 20s), which is usually just the end screen."""
    return bool(ended) or (dur > 0 and dur - pos <= max(20, 0.05 * dur))


def load_progress():
    progress = {}
    for path in PROGRESS.glob("*"):
        if path.suffix == ".tmp":
            continue
        try:
            pos, dur, ended = path.read_text().split()
            progress[path.name] = (float(pos), float(dur), ended == "1",
                                   path.stat().st_mtime)
        except (OSError, ValueError):
            pass
    return progress


def write_progress(video_id, pos, dur, when):
    """Record progress as of `when` (the file's mtime), so it can be compared
    with mpv's own saved position."""
    PROGRESS.mkdir(parents=True, exist_ok=True)
    path = PROGRESS / video_id
    path.write_text(f"{pos:.3f} {dur:.3f} 0\n")
    os.utime(path, (when, when))


def mpv_watch_later(v):
    """mpv names its saved-position file after the MD5 of the exact URL."""
    return MPV_WATCH_LATER / hashlib.md5(url(v).encode()).hexdigest().upper()


def mpv_saved_position(v):
    """(position, saved at) from mpv's watch-later file, or None."""
    path = mpv_watch_later(v)
    try:
        for line in path.read_text().splitlines():
            if line.startswith("start="):
                return float(line[6:]), path.stat().st_mtime
    except (OSError, ValueError):
        pass
    return None


class State:
    def __init__(self):
        self.subs = load(SUBS, [])          # [{id, name}]
        self.watched = load(WATCHED, {})    # {video id: unix time}, marked by hand
        self.later = load(LATER, [])        # [video], newest first
        # {video id: (position, duration, ended, saved at)}
        self.progress = load_progress()

    def duration(self, v):
        p = self.progress.get(v["id"])
        return v.get("duration") or (p[1] if p and p[1] > 0 else None)

    def finished(self, v):
        p = self.progress.get(v["id"])
        return v["id"] in self.watched or bool(p and is_finished(*p[:3]))

    def resume(self, v):
        """(position, source) where an unfinished video stopped, or None.
        tube's record and mpv's saved position can disagree (a video played
        from FreeTube, a crash, or progress imported from FreeTube), so the
        more recently saved one wins. source is "tube" or "mpv"."""
        if self.finished(v):
            return None
        saved = []
        p = self.progress.get(v["id"])
        if p:
            saved.append((p[3], p[0], "tube"))
        m = mpv_saved_position(v)
        if m:
            saved.append((m[1], m[0], "mpv"))
        if not saved:
            return None
        _, pos, source = max(saved)
        dur = self.duration(v)
        if pos < 5 or (dur and is_finished(pos, dur, False)):
            return None
        return pos, source

    def subscribed(self, channel_id):
        return any(s["id"] == channel_id for s in self.subs)

    def toggle_sub(self, channel_id, name):
        if self.subscribed(channel_id):
            self.subs = [s for s in self.subs if s["id"] != channel_id]
            save(SUBS, self.subs)
            return f"Unsubscribed from {name}"
        self.subs.append({"id": channel_id, "name": name})
        self.subs.sort(key=lambda s: s["name"].casefold())
        save(SUBS, self.subs)
        return f"Subscribed to {name}; it shows up in the feed after a refresh"

    def set_watched(self, v, on):
        """Either way the video's progress is cleared, so it next plays from
        the start: marking watched means done, unwatched means start over."""
        for path in (PROGRESS / v["id"], mpv_watch_later(v)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.progress.pop(v["id"], None)
        if on:
            self.watched[v["id"]] = time.time()
            if len(self.watched) > 20000:
                keep = sorted(self.watched.items(), key=lambda kv: kv[1])[-15000:]
                self.watched = dict(keep)
        else:
            self.watched.pop(v["id"], None)
        save(WATCHED, self.watched)

    def in_later(self, video_id):
        return any(v["id"] == video_id for v in self.later)

    def toggle_later(self, video):
        if self.in_later(video["id"]):
            self.later = [v for v in self.later if v["id"] != video["id"]]
            msg = "Removed from watch later"
        else:
            self.later.insert(0, video)
            msg = "Added to watch later"
        save(LATER, self.later)
        return msg


# --- fetching ----------------------------------------------------------------

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(xml, channel_id):
    root = ET.fromstring(xml)
    videos = []
    for entry in root.findall("a:entry", NS):
        published = entry.findtext("a:published", namespaces=NS)
        videos.append({
            "id": entry.findtext("yt:videoId", namespaces=NS),
            "title": entry.findtext("a:title", namespaces=NS) or "",
            "channel": entry.findtext("a:author/a:name", namespaces=NS) or "",
            "channel_id": entry.findtext("yt:channelId", namespaces=NS) or channel_id,
            "published": datetime.fromisoformat(published).timestamp() if published else None,
            "duration": None,
        })
    return [v for v in videos if v["id"]]


def fetch_channel_feed(channel_id):
    # UULF<id> is the channel's uploads playlist without shorts; channels that
    # only post shorts or live streams 404 there, so fall back to the full feed.
    base = "https://www.youtube.com/feeds/videos.xml"
    try:
        return parse_feed(http_get(f"{base}?playlist_id=UULF{channel_id[2:]}"), channel_id)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return parse_feed(http_get(f"{base}?channel_id={channel_id}"), channel_id)


def thumb(video_id):
    path = THUMBS / f"{video_id}.jpg"
    if path.exists():
        return path
    try:
        data = http_get(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg", timeout=10)
    except (OSError, urllib.error.URLError):
        return None
    THUMBS.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def prune_thumbs(keep):
    cutoff = time.time() - THUMB_MAX_AGE
    for path in THUMBS.glob("*.jpg"):
        try:
            if path.stem not in keep and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def feed_videos(feed):
    videos = [v for vids in feed["channels"].values() for v in vids]
    videos.sort(key=lambda v: v.get("published") or 0, reverse=True)
    return videos[:FEED_SIZE]


def refresh(wait=True):
    """Refetch every subscription's feed. Returns False if another refresh
    is already running and wait is False."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / "refresh.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        subs = load(SUBS, [])
        old = load(FEED, {"updated": 0, "channels": {}})["channels"]

        def one(sub):
            try:
                return sub["id"], fetch_channel_feed(sub["id"])
            except (OSError, urllib.error.URLError, ET.ParseError):
                return sub["id"], old.get(sub["id"], [])  # keep the last good copy

        with cf.ThreadPoolExecutor(16) as ex:
            channels = dict(ex.map(one, subs))
        feed = {"updated": time.time(), "channels": channels}
        save(FEED, feed)
        ids = [v["id"] for v in feed_videos(feed)]
        with cf.ThreadPoolExecutor(16) as ex:
            list(ex.map(thumb, ids))
        prune_thumbs(set(ids) | {v["id"] for v in load(LATER, [])})
        return True


def refresh_in_background():
    subprocess.Popen(
        [sys.executable, "-I", __file__, "refresh", "--no-wait"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


def ytdlp_list(target, limit):
    proc = subprocess.run(
        ["yt-dlp", "--no-warnings", "--flat-playlist", "-J",
         "--playlist-end", str(limit), target],
        capture_output=True, text=True, timeout=90,
    )
    if proc.returncode:
        lines = proc.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "yt-dlp failed")
    info = json.loads(proc.stdout)
    videos = []
    for e in info.get("entries") or []:
        # search results also contain channels and playlists; keep videos only
        if e.get("ie_key") not in (None, "Youtube") or not e.get("id"):
            continue
        videos.append({
            "id": e["id"],
            "title": e.get("title") or e["id"],
            "channel": e.get("channel") or e.get("uploader") or info.get("channel") or "",
            "channel_id": e.get("channel_id") or info.get("channel_id"),
            "published": e.get("timestamp") or e.get("release_timestamp"),
            "duration": e.get("duration"),
        })
    return videos


def search(query):
    return ytdlp_list(f"ytsearch{SEARCH_SIZE}:{query}", SEARCH_SIZE)


def channel_videos(channel_id):
    return ytdlp_list(f"https://www.youtube.com/channel/{channel_id}/videos", CHANNEL_SIZE)


# --- formatting --------------------------------------------------------------

def esc(text):
    return html.escape(" ".join((text or "").split()), quote=False)


def age(ts):
    s = max(0, time.time() - ts)
    for unit, size in (("y", 365 * 86400), ("mo", 30 * 86400), ("w", 7 * 86400),
                       ("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def duration(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def chip(key):
    return f"<span background='{CHIP_BG}' foreground='{CHIP_FG}'> {esc(key)} </span>"


def hint_lines(groups):
    """One line per (label, [(key, description)]) group. Labels are padded in
    IBM Plex Mono so the groups line up; it has the same line height as the
    theme's Plex Sans, which matters because rofi sizes the message as
    lines x font height and clips anything taller (so line_height is out).
    Tiny spacer lines keep the key chips of adjacent groups apart."""
    width = max(len(label) for label, _ in groups) + 2
    spacer = "\n<span size='3pt'> </span>\n"
    return spacer.join(
        f"<span font_family='IBM Plex Mono' foreground='{LABEL_FG}'>{label:<{width}}</span>"
        + "   ".join(f"{chip(key)} {esc(desc)}" for key, desc in items)
        for label, items in groups
    )


def normal_hints(kind):
    if kind == "channels":
        return hint_lines([
            ("move", [("j/k", "down/up"), ("g/G", "top/bottom"), ("i or /", "filter")]),
            ("channel", [("⏎ or l", "open"), ("s", "unsubscribe")]),
            ("go to", [("h or Esc", "back"), ("q", "quit")]),
        ])
    return hint_lines([
        ("move", [("j/k", "down/up"), ("g/G", "top/bottom"), ("Ctrl+d/u", "page"),
                  ("i or /", "search")]),
        ("video", [("⏎ or l", "play"), ("w", "watch later"), ("m", "mark watched"),
                   ("c", "channel"), ("s", "subscribe"), ("y", "copy link")]),
        ("go to", [("W", "watch later list"), ("S", "subscriptions"),
                   ("r", "refresh feed"), ("h or Esc", "back"), ("q", "quit")]),
    ])


def insert_hints():
    # three lines like normal mode's, so the window keeps its size
    return hint_lines([
        ("insert", [("Esc or ⏎", "done, keeping the filter"), ("Ctrl+j/k", "move")]),
        ("search", [("Ctrl+⏎", "search YouTube"), ("⏎", "also does when nothing matches")]),
        ("", []),
    ])


def status_line(status):
    return f"<b>{esc(status)}</b>\n" if status else ""


def progress_bar(pos, dur, cells=12):
    filled = max(1, min(cells, round(cells * pos / dur)))
    return (f"<span foreground='{LABEL_FG}' alpha='100%'>{'━' * filled}</span>"
            f"<span alpha='30%'>{'━' * (cells - filled)}</span>")


def video_text(v, state):
    meta = [esc(v.get("channel"))]
    if v.get("published"):
        meta.append(age(v["published"]))
    dur, resume = state.duration(v), state.resume(v)
    resume = resume[0] if resume else None
    if resume and dur:
        meta.append(f"{progress_bar(resume, dur)} {duration(resume)} / {duration(dur)}")
    elif resume:
        meta.append(f"resume at {duration(resume)}")
    elif dur:
        meta.append(duration(dur))
    star = "★ " if state.in_later(v["id"]) else ""
    watched = state.finished(v)
    title_alpha, meta_alpha = ("45%", "35%") if watched else ("100%", "55%")
    text = (f"<span alpha='{title_alpha}' weight='500'>{star}{esc(v['title'])}</span>\n"
            f"<span alpha='{meta_alpha}' size='small'>{' · '.join(m for m in meta if m)}</span>")
    return text


def url(v):
    return f"https://www.youtube.com/watch?v={v['id']}"


# --- rofi --------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def rofi_default_bindings():
    out = subprocess.run(["rofi", "-dump-config"], capture_output=True, text=True).stdout
    return {name: [k for k in keys.split(",") if k]
            for name, keys in re.findall(r'(kb-[a-z0-9-]+): "([^"]*)"', out)}


def binding_args(bindings, actions):
    """rofi arguments for bindings plus one kb-custom-N per action key. Keys
    we use are removed from rofi's other default bindings, since rofi
    refuses to start with a key bound twice."""
    wanted = dict(bindings)
    for n, key in enumerate(actions, 1):
        wanted[f"kb-custom-{n}"] = [key]
    taken = {k for keys in wanted.values() for k in keys}
    args = []
    for name, keys in rofi_default_bindings().items():
        kept = [k for k in keys if k not in taken]
        if name not in wanted and kept != keys:
            args += [f"-{name}", ",".join(kept)]
    for name, keys in wanted.items():
        args += [f"-{name}", ",".join(keys)]
    return args


def rasi_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mode_theme(mode, filter_text, kind="videos"):
    """-theme-str fragments for the input bar: a text box in insert mode,
    otherwise the current filter (or a hint); either way followed by a
    vim-style mode badge. One quoted string per fragment: rofi's parser
    reads from the first quote to the last one in a -theme-str."""
    # channel lists have no thumbnails: drop the icon slot so rows are short
    extra = (["element-icon { enabled: false; }", "listview { lines: 14; }"]
             if kind == "channels" else [])
    if mode == "insert":
        return extra + ["inputbar { children: [ prompt, entry, textbox-mode ]; }",
                        'textbox-mode { content: "INSERT"; background-color: @green; }']
    hint = "press i to filter" if kind == "channels" else "press i to search"
    shown = f"/{filter_text}" if filter_text else hint
    return extra + ["inputbar { children: [ prompt, textbox-filter, textbox-mode ]; }",
            f"textbox-filter {{ content: {rasi_string(shown)}; "
            f"text-color: {'@text' if filter_text else '@subtext'}; }}",
            'textbox-mode { content: "NORMAL"; }']


def rofi(mode, rows, prompt, mesg, filter_text="", selected=0, kind="videos"):
    """Show rows (an iterable of (markup, icon path or None), consumed in a
    background thread so slow sources stream in) and return
    (action, index, typed text).

    Normal mode actions: "accept" (Enter/l), "back" (Esc) or a NORMAL_ACTIONS
    action. Insert mode: "done" (Enter on a row), "search" (Enter with nothing
    matching, or Ctrl+Enter) or "normal" (Esc). In normal mode rows must
    already be filtered. -selected-row makes rofi read all input before
    showing, so it is only passed when the rows are already cached."""
    normal = mode == "normal"
    actions = list((NORMAL_ACTIONS if normal else INSERT_ACTIONS).items())
    args = ["rofi", "-dmenu", "-i", "-markup-rows", "-show-icons",
            "-sep", "\x01", "-eh", "2",
            "-p", prompt, "-mesg", mesg, "-format", "i\tf"]
    args += binding_args(NORMAL_BINDINGS if normal else INSERT_BINDINGS,
                         [key for key, _ in actions])
    if THEME.exists():
        args += ["-theme", str(THEME)]
    for fragment in mode_theme(mode, filter_text, kind):
        args += ["-theme-str", fragment]
    if not normal and filter_text:
        args += ["-filter", filter_text]
    if normal and selected:
        args += ["-selected-row", str(selected)]

    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, encoding="utf-8")

    def feed():
        try:
            for text, icon in rows:
                line = text.replace("\x01", " ")
                if icon:
                    line += f"\0icon\x1f{icon}"
                proc.stdin.write(line + "\x01")
                proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass  # rofi closed before everything loaded
        except Exception as e:  # show loader errors as a row instead of crashing
            try:
                proc.stdin.write(f"<span alpha='60%'>⚠ {esc(str(e))}</span>\x01")
            except (BrokenPipeError, ValueError):
                pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

    threading.Thread(target=feed, daemon=True).start()
    out = proc.stdout.read()
    code = proc.wait()
    index, _, typed = out.rstrip("\n").partition("\t")
    index = int(index) if index.lstrip("-").isdigit() else -1
    if code == 0:
        if normal:
            return "accept", index, typed
        return ("done" if index >= 0 else "search"), index, typed
    if 10 <= code < 10 + len(actions):
        return actions[code - 10][1], index, typed
    # Esc in normal mode; in insert mode the remaining cancel keys (Ctrl+g)
    return ("back" if normal else "normal"), index, typed


# --- views -------------------------------------------------------------------

def plain(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def matches(text, filter_text):
    """rofi's default matching: every word, case-insensitively, anywhere."""
    text = text.casefold()
    return all(word in text for word in filter_text.casefold().split())


class View:
    """A list shown in rofi: videos, or channels for the subscriptions list.
    The loader's result is cached so returning to the view after an action
    doesn't refetch."""

    def __init__(self, prompt, loader, kind="videos"):
        self.prompt = prompt
        self.loader = loader
        self.kind = kind
        self.cache = None
        self.selected_id = None
        self.filter = ""

    def items(self):
        if self.cache is None:
            self.cache = self.loader()
        return self.cache

    def render(self, item, state):
        if self.kind == "channels":
            return esc(item["name"])
        return video_text(item, state)

    def visible(self, state, filter_text):
        rendered = [(item, self.render(item, state)) for item in self.items()]
        if filter_text:
            rendered = [(i, t) for i, t in rendered if matches(plain(t), filter_text)]
        return rendered

    def selected_index(self, state):
        """Index of the remembered row among the visible ones; only known once
        the list is loaded."""
        if self.cache is None or self.selected_id is None:
            return 0
        ids = [item["id"] for item, _ in self.visible(state, self.filter)]
        return ids.index(self.selected_id) if self.selected_id in ids else 0

    def rows(self, state, shown, filter_text=""):
        """Yield rows matching filter_text, appending each item to shown as
        its row goes out, so rofi's index maps onto shown even if rofi closes
        mid-load."""
        visible = self.visible(state, filter_text)
        if self.kind != "videos":
            for item, text in visible:
                shown.append(item)
                yield text, None
            return
        # thumbnails download in parallel but rows are emitted in order
        with cf.ThreadPoolExecutor(16) as ex:
            icons = ex.map(thumb, [v["id"] for v, _ in visible])
            for (v, text), icon in zip(visible, icons):
                shown.append(v)
                yield text, icon


def feed_view(force_refresh=False):
    def loader():
        if force_refresh or not FEED.exists():
            refresh()
        return feed_videos(load(FEED, {"channels": {}}))
    return View("refreshing…" if force_refresh else "feed", loader)


def subs_view(state):
    return View(f"subscriptions ({len(state.subs)})", lambda: list(state.subs),
                kind="channels")


def channel_view(channel_id, name):
    return View(name or "channel", lambda: channel_videos(channel_id))


def play(v, state):
    PROGRESS.mkdir(parents=True, exist_ok=True)
    args = ["mpv", "--force-window=immediate", f"--force-media-title={v['title']}",
            f"--script={PROGRESS_SCRIPT}",
            f"--script-opts-append=tube-id={v['id']}",
            f"--script-opts-append=tube-progress-dir={PROGRESS}"]
    # mpv resumes from its own saved position. When tube's record is newer
    # (or mpv has none), drop mpv's and start where tube says; finished
    # videos start over rather than resume near the end.
    resume = state.resume(v)
    if state.finished(v) or (resume and resume[1] == "tube"):
        try:
            mpv_watch_later(v).unlink()
        except FileNotFoundError:
            pass
    if resume and resume[1] == "tube":
        args.append(f"--start={resume[0]:.1f}")
    subprocess.Popen(args + [url(v)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def run(stack, mode="normal"):
    state = State()
    status = ""
    while stack:
        view = stack[-1]
        shown = []
        mesg = status_line(status)
        status = ""
        if mode == "insert":
            action, i, typed = rofi("insert", view.rows(state, shown), view.prompt,
                                    mesg + insert_hints(), filter_text=view.filter,
                                    kind=view.kind)
        else:
            selected = view.selected_index(state)
            action, i, typed = rofi("normal", view.rows(state, shown, view.filter),
                                    view.prompt, mesg + normal_hints(view.kind),
                                    filter_text=view.filter, selected=selected,
                                    kind=view.kind)
        if view.prompt == "refreshing…":
            view.prompt = "feed"
        item = shown[i] if 0 <= i < len(shown) else None
        if item:
            view.selected_id = item["id"]

        if mode == "insert":
            mode = "normal"
            query = typed.strip()
            if action == "search":
                if query:
                    stack.append(View(f"search: {query}", lambda q=query: search(q)))
            else:  # Enter on a row, or Esc
                view.filter = query
            continue

        if action == "back":
            if view.filter:
                view.filter = ""  # like :noh, before going back
            else:
                stack.pop()
        elif action == "quit":
            return
        elif action == "insert":
            mode = "insert"
        elif action == "view-later":
            stack.append(View("watch later", lambda: list(state.later)))
        elif action == "view-subs":
            stack.append(subs_view(state))
        elif action == "refresh":
            stack[:] = [feed_view(force_refresh=True)]
        elif item is None:
            continue
        elif view.kind == "channels":
            if action in ("accept", "channel"):
                stack.append(channel_view(item["id"], item["name"]))
            elif action == "subscribe":
                status = state.toggle_sub(item["id"], item["name"])
                view.cache = list(state.subs)
                view.prompt = f"subscriptions ({len(state.subs)})"
        elif action == "accept":
            play(item, state)
            return
        elif action == "channel":
            if item.get("channel_id"):
                stack.append(channel_view(item["channel_id"], item.get("channel")))
        elif action == "later":
            status = state.toggle_later(item)
            if view.prompt == "watch later":
                view.cache = list(state.later)
        elif action == "subscribe":
            if item.get("channel_id"):
                status = state.toggle_sub(item["channel_id"],
                                          item.get("channel") or item["channel_id"])
        elif action == "watched":
            watched = state.finished(item)
            state.set_watched(item, on=not watched)
            status = "Marked unwatched" if watched else "Marked watched"
        elif action == "copy":
            subprocess.run(["wl-copy", url(item)], check=False)
            status = f"Copied {url(item)}"


# --- FreeTube import ---------------------------------------------------------

def read_nedb(path):
    """FreeTube's .db files are NeDB: one JSON document per line, later lines
    replace earlier ones with the same _id."""
    docs = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if "_id" not in doc:
            continue
        if doc.get("$$deleted"):
            docs.pop(doc["_id"], None)
        else:
            docs[doc["_id"]] = doc
    return list(docs.values())


def import_freetube(directory):
    state = State()
    have = {s["id"] for s in state.subs}
    added = 0
    for profile in read_nedb(directory / "profiles.db"):
        for sub in profile.get("subscriptions", []):
            if sub.get("id") and sub["id"] not in have:
                state.subs.append({"id": sub["id"], "name": sub.get("name") or sub["id"]})
                have.add(sub["id"])
                added += 1
    state.subs.sort(key=lambda s: s["name"].casefold())
    save(SUBS, state.subs)

    # FreeTube's history is every video opened, with how far it got
    finished = partial = 0
    for h in read_nedb(directory / "history.db"):
        vid = h.get("videoId")
        if not vid:
            continue
        pos = float(h.get("watchProgress") or 0)
        dur = float(h.get("lengthSeconds") or 0)
        if is_finished(pos, dur, False):
            state.watched[vid] = (h.get("timeWatched") or 0) / 1000
            finished += 1
            continue
        # imports before progress tracking marked every history entry watched
        state.watched.pop(vid, None)
        if pos >= 5:
            if vid not in state.progress:
                write_progress(vid, pos, dur, (h.get("timeWatched") or 0) / 1000)
            partial += 1
    save(WATCHED, state.watched)

    later_ids = {v["id"] for v in state.later}
    later_added = 0
    for playlist in read_nedb(directory / "playlists.db"):
        if (playlist.get("playlistName") or "").casefold() != "watch later":
            continue
        for item in sorted(playlist.get("videos", []), key=lambda x: x.get("timeAdded") or 0,
                           reverse=True):
            if item.get("videoId") in later_ids or not item.get("videoId"):
                continue
            state.later.append({
                "id": item["videoId"],
                "title": item.get("title") or item["videoId"],
                "channel": item.get("author") or "",
                "channel_id": item.get("authorId"),
                "published": (item.get("published") or 0) / 1000 or None,
                "duration": item.get("lengthSeconds"),
            })
            later_ids.add(item["videoId"])
            later_added += 1
    save(LATER, state.later)

    print(f"subscriptions: {added} added, {len(state.subs)} total")
    print(f"history: {finished} finished, {partial} in progress")
    print(f"watch later: {later_added} added")
    print("refreshing feed…")
    refresh()
    print("done")


# --- entry point -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="tube", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("search", help="search YouTube")
    p.add_argument("query", nargs="*")
    p = sub.add_parser("refresh", help="refresh the subscription feed")
    p.add_argument("--no-wait", action="store_true",
                   help="exit if another refresh is already running")
    p = sub.add_parser("import-freetube", help="import FreeTube data")
    p.add_argument("--dir", type=Path, default=HOME / ".config/FreeTube")
    args = parser.parse_args()

    if args.cmd == "refresh":
        refresh(wait=not args.no_wait)
        return
    if args.cmd == "import-freetube":
        import_freetube(args.dir)
        return

    feed = load(FEED, None)
    if feed and time.time() - feed.get("updated", 0) > STALE:
        refresh_in_background()
    stack = [feed_view()]
    if args.cmd == "search":
        query = " ".join(args.query).strip()
        if not query:
            # an empty list: Enter on the typed text searches YouTube
            stack.append(View("search YouTube", list))
            run(stack, mode="insert")
            return
        stack.append(View(f"search: {query}", lambda: search(query)))
    run(stack)


if __name__ == "__main__":
    main()
