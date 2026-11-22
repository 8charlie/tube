# tube

A rofi front-end for YouTube that plays everything in mpv.

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
| Esc or Enter | back to normal mode, keeping the filter (Enter also selects the highlighted row) |
| Enter with nothing matching, or Ctrl+Enter | search YouTube for the typed text |
| Ctrl+j / Ctrl+k | down / up |
| Ctrl+h / Ctrl+l | cursor left / right |

rofi has no modes of its own, so tube runs it with different key bindings
per mode and reopens it when you switch.

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

- Fresh googlevideo URLs currently answer 403 for their first few seconds
  and mpv gives up on the first one. Setting
  `stream-lavf-o-append=reconnect_on_http_error=403` in `mpv.conf` makes
  ffmpeg retry.
- rofi sizes every row from an empty placeholder row, so rows are as tall
  as the thumbnail is wide; that can't be fixed from the theme.
