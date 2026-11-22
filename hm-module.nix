{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.programs.tube;
in {
  options.programs.tube = {
    enable = lib.mkEnableOption "tube, a rofi front-end for YouTube";

    mpvPackage = lib.mkOption {
      type = lib.types.package;
      default =
        if config.programs.mpv.enable
        then config.programs.mpv.finalPackage
        else pkgs.mpv;
      defaultText = lib.literalExpression "config.programs.mpv.finalPackage (if enabled), else pkgs.mpv";
      description = "mpv used for playback.";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix {mpv = cfg.mpvPackage;};
      defaultText = lib.literalExpression "tube built with cfg.mpvPackage";
      description = "The tube package.";
    };

    refreshInterval = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "20min";
      description = ''
        How often a systemd user timer refreshes the feed and thumbnails so
        the picker opens instantly. null disables the timer.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [cfg.package];

    systemd.user.services.tube-refresh = lib.mkIf (cfg.refreshInterval != null) {
      Unit.Description = "Refresh the tube subscription feed";
      Service = {
        Type = "oneshot";
        ExecStart = "${lib.getExe cfg.package} refresh --no-wait";
      };
    };

    systemd.user.timers.tube-refresh = lib.mkIf (cfg.refreshInterval != null) {
      Unit.Description = "Refresh the tube subscription feed";
      Timer = {
        OnStartupSec = "1min";
        OnUnitActiveSec = cfg.refreshInterval;
      };
      Install.WantedBy = ["timers.target"];
    };
  };
}
