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
import html
import json
import os
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
FEED = CACHE / "feed.json"
THEME = Path(os.environ.get("TUBE_THEME") or Path(__file__).with_name("tube.rasi"))

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

# rofi custom key slot -> (key, action). Shown as hints under the prompt.
KEYS = {
    1: ("Alt+a", "audio"),
    2: ("Alt+c", "channel"),
    3: ("Alt+w", "later"),
    4: ("Alt+s", "subscribe"),
    5: ("Alt+m", "watched"),
    6: ("Alt+y", "copy"),
    7: ("Alt+l", "view-later"),
    8: ("Alt+u", "view-subs"),
    9: ("Alt+r", "refresh"),
    # Enter on a matching row plays it; this searches for the typed text
    10: ("Control+Return", "search"),
}
ACTION_KEY = {action: key for key, action in KEYS.values()}

# Ctrl+h/j/k/l act as arrow keys. rofi binds all four by default (backspace,
# accept, delete to end of line, complete), so those lose their Ctrl variants.
ROFI_BINDINGS = {
    "kb-row-up": "Up,Control+p,Control+k",
    "kb-row-down": "Down,Control+n,Control+j",
    "kb-move-char-back": "Left,Control+b,Control+h",
    "kb-move-char-forward": "Right,Control+f,Control+l",
    "kb-remove-char-back": "BackSpace,Shift+BackSpace",
    "kb-accept-entry": "Control+m,Return,KP_Enter",
    "kb-remove-to-eol": "",
    "kb-mode-complete": "",
    # freed for our own keys
    "kb-screenshot": "",
    "kb-accept-custom": "",
    "kb-move-word-back": "Control+Left",
    "kb-move-word-forward": "Control+Right",
}
HINTS = (
    "⏎ play · {audio} audio · {channel} channel · {later} watch later · "
    "{subscribe} (un)subscribe · {watched} watched · {copy} copy link\n"
    "{view-later} watch later list · {view-subs} subscriptions · "
    "{refresh} refresh · Ctrl+⏎ (or ⏎ with no match) searches YouTube · "
    "Ctrl+j/k move · Esc back"
).format(**ACTION_KEY)


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


class State:
    def __init__(self):
        self.subs = load(SUBS, [])          # [{id, name}]
        self.watched = load(WATCHED, {})    # {video id: unix time}
        self.later = load(LATER, [])        # [video], newest first

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

    def mark_watched(self, video_id, on=True):
        if on:
            self.watched[video_id] = time.time()
            if len(self.watched) > 20000:
                keep = sorted(self.watched.items(), key=lambda kv: kv[1])[-15000:]
                self.watched = dict(keep)
        else:
            self.watched.pop(video_id, None)
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


def video_row(v, state, icon):
    meta = [esc(v.get("channel"))]
    if v.get("published"):
        meta.append(age(v["published"]))
    if v.get("duration"):
        meta.append(duration(v["duration"]))
    star = "★ " if state.in_later(v["id"]) else ""
    watched = v["id"] in state.watched
    title_alpha, meta_alpha = ("45%", "35%") if watched else ("100%", "55%")
    text = (f"<span alpha='{title_alpha}' weight='500'>{star}{esc(v['title'])}</span>\n"
            f"<span alpha='{meta_alpha}' size='small'>{' · '.join(m for m in meta if m)}</span>")
    return text, icon


def url(v):
    return f"https://www.youtube.com/watch?v={v['id']}"


# --- rofi --------------------------------------------------------------------

def rofi(rows, prompt, mesg, selected=0, filter_text=""):
    """Show rows (an iterable of (markup, icon path or None), consumed in a
    background thread so slow sources stream in) and return
    (action, index, typed text). action is "accept" (a row), "custom" (typed
    text matching no row) or one of the KEYS actions; None means Esc.
    -selected-row makes rofi read all input before showing, so it is only
    passed when the rows are already cached."""
    args = [
        "rofi", "-dmenu", "-i", "-markup-rows", "-show-icons",
        "-sep", "\x01", "-eh", "2",
        "-p", prompt, "-mesg", mesg, "-format", "i\tf",
    ]
    for name, keys in ROFI_BINDINGS.items():
        args += [f"-{name}", keys]
    if THEME.exists():
        args += ["-theme", str(THEME)]
    for slot, (key, _) in KEYS.items():
        args += [f"-kb-custom-{slot}", key]
    if filter_text:
        args += ["-filter", filter_text]
    elif selected:
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
        return ("accept" if index >= 0 else "custom"), index, typed
    if 10 <= code < 10 + len(KEYS):
        return KEYS[code - 9][1], index, typed
    return None, index, typed


# --- views -------------------------------------------------------------------

class View:
    """A list of videos shown in rofi. The loader's result is cached so
    returning to the view after an action doesn't refetch."""

    def __init__(self, prompt, loader):
        self.prompt = prompt
        self.loader = loader
        self.cache = None
        self.selected = 0
        self.filter = ""

    def rows(self, state, shown):
        """Yield rows, appending each video to shown as its row goes out, so
        rofi's index maps onto shown even if rofi closes mid-load."""
        if self.cache is None:
            self.cache = self.loader()
        videos = list(self.cache)
        # thumbnails download in parallel but rows are emitted in order
        with cf.ThreadPoolExecutor(16) as ex:
            for v, icon in zip(videos, ex.map(thumb, [v["id"] for v in videos])):
                shown.append(v)
                yield video_row(v, state, icon)


def feed_view(force_refresh=False):
    def loader():
        if force_refresh or not FEED.exists():
            refresh()
        return feed_videos(load(FEED, {"channels": {}}))
    return View("refreshing…" if force_refresh else "feed", loader)


def play(v, state, audio=False):
    state.mark_watched(v["id"])
    args = ["mpv", "--force-window=immediate", f"--force-media-title={v['title']}"]
    if audio:
        args += ["--ytdl-format=bestaudio/best", "--vid=no"]
    subprocess.Popen(args + [url(v)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def subs_menu(state, stack):
    """The subscriptions list: ⏎ opens a channel, Alt+s unsubscribes."""
    status, selected = "", 0
    while True:
        subs = list(state.subs)
        rows = [(esc(s["name"]), None) for s in subs]
        mesg = (status + "\n" if status else "") + \
            f"⏎ open channel · {ACTION_KEY['subscribe']} unsubscribe · Esc back"
        action, i, _ = rofi(rows, f"subscriptions ({len(subs)})", mesg, selected)
        if action is None:
            return
        if not 0 <= i < len(subs):
            continue
        sub, selected = subs[i], i
        if action in ("accept", "channel"):
            stack.append(View(sub["name"], lambda cid=sub["id"]: channel_videos(cid)))
            return
        if action == "subscribe":
            status = state.toggle_sub(sub["id"], sub["name"])
            selected = min(i, len(state.subs) - 1)


def run(stack):
    state = State()
    status = ""
    while stack:
        view = stack[-1]
        mesg = (f"<b>{esc(status)}</b>\n" if status else "") + HINTS
        status = ""
        shown = []
        cached = view.cache is not None
        action, i, typed = rofi(view.rows(state, shown), view.prompt, mesg,
                                view.selected if cached else 0, view.filter)
        if view.prompt == "refreshing…":
            view.prompt = "feed"
        v = shown[i] if 0 <= i < len(shown) else None
        if v:
            view.selected, view.filter = i, typed

        if action is None:
            stack.pop()
        elif action in ("custom", "search"):
            if typed.strip():
                view.filter = ""
                stack.append(View(f"search: {typed.strip()}", lambda q=typed.strip(): search(q)))
        elif action == "view-later":
            stack.append(View("watch later", lambda: list(state.later)))
        elif action == "view-subs":
            subs_menu(state, stack)
        elif action == "refresh":
            stack[:] = [feed_view(force_refresh=True)]
        elif v is None:
            continue
        elif action == "accept":
            play(v, state)
            return
        elif action == "audio":
            play(v, state, audio=True)
            return
        elif action == "channel":
            if v.get("channel_id"):
                stack.append(View(v["channel"] or "channel",
                                  lambda cid=v["channel_id"]: channel_videos(cid)))
        elif action == "later":
            status = state.toggle_later(v)
            if view.prompt == "watch later":
                view.cache = list(state.later)
        elif action == "subscribe":
            if v.get("channel_id"):
                status = state.toggle_sub(v["channel_id"], v.get("channel") or v["channel_id"])
        elif action == "watched":
            watched = v["id"] in state.watched
            state.mark_watched(v["id"], on=not watched)
            status = "Marked unwatched" if watched else "Marked watched"
        elif action == "copy":
            subprocess.run(["wl-copy", url(v)], check=False)
            status = f"Copied {url(v)}"


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

    history = read_nedb(directory / "history.db")
    for h in history:
        if h.get("videoId"):
            state.watched[h["videoId"]] = (h.get("timeWatched") or 0) / 1000
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
    print(f"history: {len(history)} videos marked watched")
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
            _, _, query = rofi([], "search YouTube", "type a query and press ⏎")
            query = query.strip()
            if not query:
                return
        stack.append(View(f"search: {query}", lambda: search(query)))
    run(stack)


if __name__ == "__main__":
    main()
