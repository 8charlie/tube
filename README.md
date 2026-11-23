# tube

A Quickshell front-end for YouTube that plays everything in mpv.

- **Feed:** your subscriptions, newest first, from YouTube's per-channel RSS
  feeds. No API key and no Invidious/Piped instance; shorts are left out.
- **Search and channel pages:** yt-dlp.
- **Playback:** mpv, which itself goes through yt-dlp, so your mpv scripts
  (SponsorBlock, uosc, …) apply.

```
tube                    open the picker on the subscription feed
tube search [QUERY]     open the picker on a YouTube search
tube refresh            refresh the feed and thumbnails
tube import-freetube    import subscriptions, history and watch later
```

## Keys

Vim style. tube opens in **normal mode**, where there's no text box and
plain letters are actions. `i` (or `/`) switches to **insert mode** to type.

Normal mode:

| Key | Action |
|---|---|
| j / k | down / up (also Ctrl+j / Ctrl+k) |
| g / G | top / bottom |
| Ctrl+d / Ctrl+u | page down / up |
| i or / | insert mode: filter the list or search YouTube |
| Enter or l | play (in the subscriptions list: open the channel) |
| w | add to / remove from watch later |
| m | toggle watched (either way, clears the video's progress) |
| c | open the video's channel |
| s | subscribe / unsubscribe to the video's channel |
| y | copy the link |
| W | watch later list |
| S | subscriptions list |
| r | refresh the feed now |
| h or Esc | back (Esc first clears an active filter) |
| q | quit |

Insert mode:

| Key | Action |
|---|---|
| typing | filters the list |
| Enter | search YouTube for the typed text |
| Esc or Ctrl+Enter | back to normal mode, keeping the filter (Ctrl+Enter searches instead when nothing matches) |
| Ctrl+j / Ctrl+k | down / up |
| Ctrl+h / Ctrl+l | cursor left / right |

The mouse works too: click a row to select it, double-click to play.

## Front-end

`tube` opens a [Quickshell](https://quickshell.org) window (`qml/`) on the
overlay layer, in the middle of the screen, with exclusive keyboard focus
like a launcher. It runs `tube serve` as its backend and talks to it in JSON
lines (the protocol is described in `tube.py`). Actions update rows in place,
and search thumbnails fill in as they download.

The layer-shell namespace is `tube`, so niri can give it a shadow:

```kdl
layer-rule {
    match namespace="^tube$"
    shadow { on; }
    geometry-corner-radius 14
}
```

For scripts, a running tube takes keys over IPC:
`quickshell ipc --path <tube's qml dir> call tube key j`.

## Progress

Videos played from tube carry a small mpv script (`tube_progress.lua`) that
records position and duration every 10s, on quit and at the end. The feed
shows a progress bar for unfinished videos and dims finished ones (played to
the end, or stopped in the last 5% / 20s). mpv's own `save-position-on-quit`
does the resuming; when tube's record is newer than mpv's (or mpv has none,
e.g. for progress imported from FreeTube), tube passes `--start` instead.

## Files

- `~/.local/share/tube/`: `subscriptions.json`, `watched.json`, `later.json`,
  `progress/` (one `<position> <duration> <ended>` file per video)
- `~/.cache/tube/`: `feed.json`, `thumbs/`

## Nix

The flake exposes `packages.<system>.default` and a home-manager module:

```nix
{
  inputs.tube.url = "github:8charlie/tube";

  # in a home-manager configuration
  imports = [inputs.tube.homeManagerModules.default];
  programs.tube.enable = true;
}
```

The module uses `programs.mpv.finalPackage` when `programs.mpv` is enabled
(so tube plays through your mpv scripts) and runs a systemd user timer that
refreshes the feed every `programs.tube.refreshInterval` (default `20min`).

## Notes

- When mpv can't play a video, tube says why in a desktop notification.
  YouTube's "Sign in to confirm you're not a bot" usually means it has
  flagged your IP, which happens a lot with VPNs; a different server, or
  `ytdl-raw-options=cookies-from-browser=<browser>` in `mpv.conf`, gets
  around it.
- Recommended `mpv.conf` line for YouTube:
  `stream-lavf-o=reconnect_on_http_error=403,request_size=10485760,multiple_requests=1`
  - Fresh googlevideo URLs answer 403 for their first few seconds and mpv
    gives up on the first one; `reconnect_on_http_error` makes ffmpeg retry.
  - googlevideo throttles a single open-ended request (how ffmpeg streams by
    default) to about 1 Mbps, barely above playback speed, so mpv buffers
    only seconds ahead and stalls. `request_size` fetches 10 MB ranges like
    yt-dlp does, which come at full speed.
