{
  lib,
  runCommand,
  makeWrapper,
  python3,
  mpv,
  rofi,
  wl-clipboard,
  yt-dlp,
}:
# Pass your own mpv (e.g. home-manager's programs.mpv.finalPackage) with
# `.override { mpv = ...; }` so tube plays through your scripts and config.
runCommand "tube" {
  nativeBuildInputs = [makeWrapper];
  meta = {
    description = "rofi front-end for YouTube that plays everything in mpv";
    platforms = lib.platforms.linux;
    mainProgram = "tube";
  };
} ''
  install -Dm644 ${./tube.py} $out/share/tube/tube.py
  install -Dm644 ${./tube.rasi} $out/share/tube/tube.rasi
  makeWrapper ${python3}/bin/python3 $out/bin/tube \
    --add-flags "-I $out/share/tube/tube.py" \
    --prefix PATH : ${lib.makeBinPath [mpv rofi wl-clipboard yt-dlp]}
''
