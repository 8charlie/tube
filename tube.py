"""tube: a Quickshell front-end for YouTube that plays everything in mpv.

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
import hashlib
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
PROGRESS = DATA / "progress"    # one "<position> <duration> <ended>" file per video
FEED = CACHE / "feed.json"
QML = Path(os.environ.get("TUBE_QML") or Path(__file__).with_name("qml"))
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


def url(v):
    return f"https://www.youtube.com/watch?v={v['id']}"


# --- backend for the front-end ------------------------------------------------
# `tube serve` is the backend of the Quickshell front-end (qml/), which starts
# it and talks JSON lines over stdin/stdout. Requests carry a "req" number
# that the reply echoes:
#
#   {"req": 1, "cmd": "list", "kind": "feed" | "later" | "subs" | "search"
#    | "channel", "query": ..., "channel_id": ..., "refresh": bool}
#     -> {"req": 1, "items": [row, ...]}
#   {"req": 2, "cmd": "later" | "watched" | "subscribe" | "copy" | "play",
#    "id": video id}  (or a channel id with "channel": true, for "subscribe")
#     -> {"req": 2, "status": "...", "items": [rows that changed]}
#
# Either reply can be {"req": n, "error": "..."} instead. Unprompted events:
#
#   {"event": "thumb", "id": video id, "path": ...}  a thumbnail downloaded
#   {"event": "feed-updated"}                        a background refresh finished

def video_row(v, state, subscribed):
    dur, resume = state.duration(v), state.resume(v)
    pos = resume[0] if resume else 0
    path = THUMBS / f"{v['id']}.jpg"
    return {
        "id": v["id"],
        "title": " ".join((v.get("title") or v["id"]).split()),
        "channel": v.get("channel") or "",
        "channel_id": v.get("channel_id") or "",
        "age": age(v["published"]) if v.get("published") else "",
        "length": duration(dur) if dur else "",
        "position": duration(pos) if pos else "",
        "progress": min(1, pos / dur) if pos and dur else 0,
        "watched": state.finished(v),
        "later": state.in_later(v["id"]),
        "subscribed": v.get("channel_id") in subscribed,
        "thumb": str(path) if path.exists() else "",
    }


def channel_row(sub, latest):
    ts = latest.get(sub["id"])
    return {"id": sub["id"], "title": sub["name"], "subscribed": True,
            "age": f"latest upload {age(ts)}" if ts else ""}


class Server:
    def __init__(self):
        self.state = State()
        self.lock = threading.Lock()   # state, known and fetching
        self.out = threading.Lock()
        self.known = {}                # video id -> video, for every row sent
        self.fetching = set()          # thumbnails being downloaded
        self.thumbs = cf.ThreadPoolExecutor(16)
        self.refreshing = False

    def emit(self, msg):
        line = json.dumps(msg, ensure_ascii=False)
        with self.out:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def run(self):
        """Lists run in a pool, since searches take seconds. Actions are quick
        and run in order here, so they're all done by the time the front-end
        closes stdin, which it does right after the last one."""
        lists = cf.ThreadPoolExecutor(4)
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("cmd") == "list":
                lists.submit(self.reply, msg, self.list)
            else:
                self.reply(msg, self.action)
        os._exit(0)  # don't wait for searches or thumbnails nobody will see

    def reply(self, msg, handler):
        out = {"req": msg.get("req")}
        try:
            out.update(handler(msg))
        except Exception as e:
            out["error"] = str(e) or type(e).__name__
        self.emit(out)

    def list(self, msg):
        kind = msg.get("kind")
        videos = None
        if kind == "feed":
            if msg.get("refresh") or not FEED.exists():
                refresh()
            feed = load(FEED, {"updated": 0, "channels": {}})
            if time.time() - feed.get("updated", 0) > STALE:
                self.refresh_in_background(feed.get("updated", 0))
            videos = feed_videos(feed)
        elif kind == "search":
            videos = search(msg["query"])
        elif kind == "channel":
            videos = channel_videos(msg["channel_id"])
        elif kind not in ("later", "subs"):
            raise ValueError(f"unknown list {kind!r}")

        with self.lock:
            # reread what mpv, the refresh timer or another tube changed
            state = self.state = State()
            if kind == "subs":
                latest = {}
                for cid, vids in load(FEED, {"channels": {}})["channels"].items():
                    latest[cid] = max((v.get("published") or 0 for v in vids), default=0)
                return {"items": [channel_row(s, latest) for s in state.subs]}
            if kind == "later":
                videos = state.later
            subscribed = {s["id"] for s in state.subs}
            rows = []
            for v in videos:
                self.known[v["id"]] = v
                rows.append(video_row(v, state, subscribed))
                if not rows[-1]["thumb"] and v["id"] not in self.fetching:
                    self.fetching.add(v["id"])
                    self.thumbs.submit(self.fetch_thumb, v["id"])
            return {"items": rows}

    def fetch_thumb(self, video_id):
        path = thumb(video_id)
        with self.lock:
            self.fetching.discard(video_id)
        if path:
            self.emit({"event": "thumb", "id": video_id, "path": str(path)})

    def refresh_in_background(self, updated):
        """Refresh in a separate process, like the timer does, so it finishes
        even if tube closes first; tell the front-end if the feed changed."""
        with self.lock:
            if self.refreshing:
                return
            self.refreshing = True

        def wait():
            subprocess.run([sys.executable, "-I", __file__, "refresh", "--no-wait"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, start_new_session=True)
            # if the timer's refresh was running, --no-wait left it to that one
            with open(CACHE / "refresh.lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
            if load(FEED, {}).get("updated", 0) > updated:
                self.emit({"event": "feed-updated"})

        threading.Thread(target=wait, daemon=True).start()

    def action(self, msg):
        cmd, key = msg.get("cmd"), msg.get("id")
        with self.lock:
            state = self.state
            if msg.get("channel"):  # a row in the subscriptions list
                if cmd != "subscribe":
                    raise ValueError(f"{cmd!r} needs a video")
                name = next((s["name"] for s in state.subs if s["id"] == key), key)
                return {"status": state.toggle_sub(key, name)}
            v = self.known.get(key)
            if v is None:
                raise ValueError(f"unknown video {key!r}")
            if cmd == "play":
                play(v, state)
                return {"status": "Playing"}
            if cmd == "copy":
                subprocess.run(["wl-copy", url(v)], check=False)
                return {"status": f"Copied {url(v)}"}
            changed = [v]
            if cmd == "later":
                status = state.toggle_later(v)
            elif cmd == "watched":
                watched = state.finished(v)
                state.set_watched(v, on=not watched)
                status = "Marked unwatched" if watched else "Marked watched"
            elif cmd == "subscribe":
                if not v.get("channel_id"):
                    return {"status": "This video's channel isn't known"}
                status = state.toggle_sub(v["channel_id"], v.get("channel") or v["channel_id"])
                changed = [k for k in self.known.values()
                           if k.get("channel_id") == v["channel_id"]]
            else:
                raise ValueError(f"unknown command {cmd!r}")
            subscribed = {s["id"] for s in state.subs}
            return {"status": status,
                    "items": [video_row(c, state, subscribed) for c in changed]}


def launch(start):
    """Open the front-end, which starts `tube serve` as its backend. start
    is {"search": query} to open on a search ("" to type one)."""
    env = dict(os.environ, TUBE_START=json.dumps(start), TUBE_BACKEND=json.dumps(
        [sys.executable, "-I", str(Path(__file__).resolve()), "serve"]))
    os.execvpe("quickshell", ["quickshell", "--no-duplicate", "--path", str(QML)], env)


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
    sub.add_parser("serve", help="the front-end's backend (JSON lines on stdin/stdout)")
    args = parser.parse_args()

    if args.cmd == "refresh":
        refresh(wait=not args.no_wait)
        return
    if args.cmd == "import-freetube":
        import_freetube(args.dir)
        return

    if args.cmd == "serve":
        Server().run()
        return
    launch({"search": " ".join(args.query).strip()} if args.cmd == "search" else {})


if __name__ == "__main__":
    main()
