# tube

A Quickshell front-end for YouTube that plays everything in mpv.

- **Feed:** your subscriptions from YouTube's RSS feed.
- **Search and channel pages:** yt-dlp.
- **Playback:** mpv, with a small script (`tube_progress.lua`) that saves
  your progress.

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
