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

| Key | Action |
|---|---|
| Enter | play (with typed text matching nothing: search YouTube) |
| Ctrl+Enter | search YouTube for the typed text |
| Ctrl+h / j / k / l | left / down / up / right |
| Alt+a | play audio only |
| Alt+c | open the video's channel |
| Alt+w | add to / remove from watch later |
| Alt+s | subscribe / unsubscribe to the video's channel |
| Alt+m | toggle watched |
| Alt+y | copy the link |
| Alt+l | watch later list |
| Alt+u | subscriptions list |
| Alt+r | refresh the feed now |
| Esc | back / close |

## Files

- `~/.local/share/tube/`: `subscriptions.json`, `watched.json`, `later.json`
- `~/.cache/tube/`: `feed.json`, `thumbs/`

## Nix

The flake exposes `packages.<system>.default` and a home-manager module:

```nix
{
  inputs.tube.url = "github:<you>/tube";  # or git+file:///path/to/tube

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
