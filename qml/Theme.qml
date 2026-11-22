pragma Singleton

import QtQuick
import Quickshell

// Nord (nordtheme.com): Polar Night backgrounds, Snow Storm text, a Frost
// accent
Singleton {
    readonly property color base: "#2e3440"      // nord0
    readonly property color mantle: "#242933"    // below nord0, behind thumbnails
    readonly property color surface0: "#3b4252"  // nord1
    readonly property color surface1: "#434c5e"  // nord2
    readonly property color text: "#e5e9f0"      // nord5
    readonly property color subtext: "#a5abb6"   // nord4 faded into nord0
    readonly property color accent: "#88c0d0"    // nord8
    readonly property color green: "#a3be8c"     // nord14

    readonly property string sans: "IBM Plex Sans"
    readonly property string mono: "IBM Plex Mono"
}
