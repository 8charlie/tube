// tube's front-end: a layer-shell picker over the `tube serve` backend
// (see the protocol in tube.py). tube starts it with TUBE_BACKEND, the
// backend's command as a JSON list, and TUBE_START, {"search": query} to
// open on a search ("" to type one).

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root

    // --- views ---------------------------------------------------------------
    // A view is one list on the stack, a plain object (see makeView), so after
    // changing one call show() to put it on screen. kind is feed, later, subs,
    // search, channel or prompt (an empty list to type a search into).
    property var stack: []
    property var view: null
    // copied from the shown view by show(), for bindings
    property string kind: ""
    property string title: ""
    property string filter: ""
    property bool loading: true
    property string error: ""
    readonly property bool channels: kind === "subs"

    property string mode: "normal"   // or "insert"
    property var beforeInsert: ({})   // the view's filter and selection then
    property string status: ""
    property bool populating: false
    property bool quitting: false

    property int nextReq: 1
    property var callbacks: ({})
    property int pendingActions: 0
    property var pendingThumbs: ({})

    ListModel { id: rows }

    function makeView(kind, extra) {
        return Object.assign({
            kind: kind, query: "", channelId: "", name: "",
            items: null, error: "", filter: "", refreshing: false,
            selectedId: "", selectedIndex: 0,
        }, extra || {});
    }

    function viewTitle(v) {
        switch (v.kind) {
        case "feed": return v.refreshing ? "refreshing…" : "feed";
        case "later": return "watch later";
        case "subs": return v.items ? `subscriptions (${v.items.length})` : "subscriptions";
        case "search": return "search: " + v.query;
        case "channel": return v.name || "channel";
        case "prompt": return "search YouTube";
        }
        return v.kind;
    }

    // A backend row with every role present, so ListModel's types stay fixed.
    function row(it) {
        return {
            key: it.id, title: it.title || "", channel: it.channel || "",
            channelId: it.channel_id || "", age: it.age || "", length: it.length || "",
            position: it.position || "", progress: it.progress || 0,
            watched: !!it.watched, later: !!it.later, subscribed: !!it.subscribed,
            thumb: it.thumb || "",
        };
    }

    // every word, case-insensitively, anywhere in the row
    function matches(it, filter) {
        const words = filter.toLowerCase().split(/\s+/).filter(w => w);
        if (!words.length)
            return true;
        const text = [it.title, it.channel, it.age, it.length].join(" ").toLowerCase();
        return words.every(w => text.includes(w));
    }

    // Put the top view on screen. With keep, the selection stays on the same
    // item, or in the same place if the item left the list; otherwise it
    // goes to the top.
    function show(keep) {
        const v = view = stack[stack.length - 1];
        kind = v.kind;
        title = viewTitle(v);
        filter = v.filter;
        loading = v.items === null || v.refreshing;
        error = v.error;
        const items = (v.items || []).filter(it => matches(it, v.filter));
        let i = 0;
        if (keep && v.selectedId) {
            i = items.findIndex(it => it.key === v.selectedId);
            if (i < 0)
                i = Math.min(v.selectedIndex, items.length - 1);
        }
        populating = true;
        rows.clear();
        if (items.length)
            rows.append(items);
        list.currentIndex = items.length ? i : -1;
        populating = false;
        if (items.length) {
            remember();
            list.positionViewAtIndex(i, ListView.Center);
        }
    }

    function remember() {
        const it = current();
        if (view && it) {
            view.selectedId = it.key;
            view.selectedIndex = list.currentIndex;
        }
    }

    function push(v) {
        stack.push(v);
        load(v);
        show(true);
    }

    function load(v, refresh) {
        if (v.kind === "prompt") {
            v.items = [];
            return;
        }
        v.refreshing = !!refresh;
        request({cmd: "list", kind: v.kind, query: v.query, channel_id: v.channelId,
                 refresh: !!refresh}, reply => {
            v.refreshing = false;
            if (reply.error !== undefined) {
                v.error = reply.error;
                if (v.items && v.items.length)
                    say("⚠ " + reply.error);
            } else {
                v.items = reply.items.map(row);
                v.error = "";
            }
            if (!v.items)
                v.items = [];
            if (v === root.view)
                show(true);
        });
    }

    // Apply changed rows from the backend to every view and to the screen.
    function update(changed) {
        const byKey = {};
        for (const it of changed)
            byKey[it.key] = it;
        for (const v of stack)
            for (const it of v.items || [])
                if (byKey[it.key])
                    Object.assign(it, byKey[it.key]);
        for (let i = 0; i < rows.count; i++) {
            const it = byKey[rows.get(i).key];
            if (it)
                rows.set(i, it);
        }
    }

    function applyThumbs() {
        const thumbs = pendingThumbs;
        pendingThumbs = {};
        for (const v of stack)
            for (const it of v.items || [])
                if (thumbs[it.key])
                    it.thumb = thumbs[it.key];
        for (let i = 0; i < rows.count; i++) {
            const path = thumbs[rows.get(i).key];
            if (path)
                rows.setProperty(i, "thumb", path);
        }
    }

    function current() {
        return list.currentIndex >= 0 && list.currentIndex < rows.count
            ? rows.get(list.currentIndex) : null;
    }

    function say(text) {
        status = text;
        statusTimer.restart();
    }

    // --- actions -------------------------------------------------------------

    function moveTo(i) {
        if (!rows.count)
            return;
        list.currentIndex = Math.max(0, Math.min(rows.count - 1, i));
        list.positionViewAtIndex(list.currentIndex, ListView.Contain);
    }

    function move(by) {
        moveTo(list.currentIndex + by);
    }

    function page(by) {
        const rowHeight = (channels ? 44 : 102) + list.spacing;
        move(by * Math.max(1, Math.floor(list.height / rowHeight)));
    }

    function accept() {
        const it = current();
        if (!it)
            return;
        if (channels)
            push(makeView("channel", {channelId: it.key, name: it.title}));
        else
            play(it);
    }

    function play(it) {
        request({cmd: "play", id: it.key}, reply => {
            if (reply.error !== undefined)
                console.warn("tube: can't play:", reply.error);
        });
        quit();
    }

    function openChannel() {
        const it = current();
        if (!it)
            return;
        if (channels)
            accept();
        else if (it.channelId)
            push(makeView("channel", {channelId: it.channelId, name: it.channel}));
    }

    function openList(kind) {
        if (view.kind !== kind)
            push(makeView(kind));
    }

    // later, watched, subscribe or copy on the selected row
    function act(cmd) {
        const it = current();
        if (!it || (channels && cmd !== "subscribe"))
            return;
        const v = view;
        request({cmd: cmd, id: it.key, channel: channels}, reply => {
            if (reply.error !== undefined)
                return say("⚠ " + reply.error);
            say(reply.status);
            if (reply.items)
                update(reply.items.map(row));
            // the row leaves these lists
            if (v.kind === "subs" || (v.kind === "later" && cmd === "later"))
                load(v);
        });
    }

    function refreshFeed() {
        const feed = stack[0];
        stack = [feed];
        load(feed, true);
        show(true);
    }

    function back() {
        if (view.filter) {  // like :noh, before going back
            view.filter = "";
            show(true);
        } else if (stack.length > 1) {
            stack.pop();
            show(true);
        } else {
            quit();
        }
    }

    // Close once the backend has done every action sent, since it goes down
    // with us.
    function quit() {
        quitting = true;
        window.visible = false;
        if (!pendingActions)
            Qt.quit();
        else
            quitAnyway.start();
    }

    // --- modes and keys ------------------------------------------------------

    function enterInsert() {
        beforeInsert = {filter: view.filter, selectedId: view.selectedId,
                        selectedIndex: view.selectedIndex};
        input.text = view.filter;
        input.cursorPosition = input.text.length;
        mode = "insert";
        input.forceActiveFocus();
    }

    function leaveInsert() {  // the typed text stays as the filter
        mode = "normal";
        keys.forceActiveFocus();
    }

    function searchTyped() {
        const query = input.text.trim();
        if (!query)  // nothing to search for: done, with the (cleared) filter
            return leaveInsert();
        Object.assign(view, beforeInsert);  // what was typed is a query, not a filter
        leaveInsert();
        if (view.kind === "prompt")
            stack.pop();
        push(makeView("search", {query: query}));
    }

    function typed() {
        if (mode === "insert" && view) {
            view.filter = input.text;
            show(false);
        }
    }

    // Letters are matched by the text they type, so W is W however the
    // keyboard makes it; Ctrl combinations by key.
    function normalKey(key, text, ctrl) {
        if (ctrl) {
            switch (key) {
            case Qt.Key_J: case Qt.Key_N: move(1); return true;
            case Qt.Key_K: case Qt.Key_P: move(-1); return true;
            case Qt.Key_D: page(1); return true;
            case Qt.Key_U: page(-1); return true;
            case Qt.Key_G: back(); return true;
            }
            return false;
        }
        switch (key) {
        case Qt.Key_Down: move(1); return true;
        case Qt.Key_Up: move(-1); return true;
        case Qt.Key_Home: moveTo(0); return true;
        case Qt.Key_End: moveTo(rows.count - 1); return true;
        case Qt.Key_PageDown: page(1); return true;
        case Qt.Key_PageUp: page(-1); return true;
        case Qt.Key_Return: case Qt.Key_Enter: accept(); return true;
        case Qt.Key_Escape: back(); return true;
        }
        const action = {
            "j": () => move(1),
            "k": () => move(-1),
            "g": () => moveTo(0),
            "G": () => moveTo(rows.count - 1),
            "i": enterInsert,
            "/": enterInsert,
            "l": accept,
            "h": back,
            "w": () => act("later"),
            "m": () => act("watched"),
            "c": openChannel,
            "s": () => act("subscribe"),
            "y": () => act("copy"),
            "W": () => openList("later"),
            "S": () => openList("subs"),
            "r": refreshFeed,
            "q": quit,
        }[text];
        if (!action)
            return false;
        action();
        return true;
    }

    // Anything not handled here is typing, which the text box takes.
    function insertKey(key, text, ctrl) {
        const enter = key === Qt.Key_Return || key === Qt.Key_Enter;
        if (key === Qt.Key_Escape || (ctrl && key === Qt.Key_G))
            leaveInsert();
        else if (enter && ctrl)
            rows.count ? leaveInsert() : searchTyped();
        else if (enter || (ctrl && key === Qt.Key_M))
            searchTyped();
        else if (key === Qt.Key_Down || (ctrl && (key === Qt.Key_J || key === Qt.Key_N)))
            move(1);
        else if (key === Qt.Key_Up || (ctrl && (key === Qt.Key_K || key === Qt.Key_P)))
            move(-1);
        else if (ctrl && (key === Qt.Key_H || key === Qt.Key_B))
            input.cursorPosition = Math.max(0, input.cursorPosition - 1);
        else if (ctrl && (key === Qt.Key_L || key === Qt.Key_F))
            input.cursorPosition = Math.min(input.text.length, input.cursorPosition + 1);
        else
            return false;
        return true;
    }

    // A key by name, for `quickshell ipc call tube key ...`: a character, or
    // Return, Escape, Up, Down, Home, End, PageUp or PageDown, optionally
    // after "ctrl+".
    function press(name) {
        const ctrl = name.startsWith("ctrl+");
        if (ctrl)
            name = name.slice(5);
        const named = {
            Return: Qt.Key_Return, Escape: Qt.Key_Escape, Up: Qt.Key_Up, Down: Qt.Key_Down,
            Home: Qt.Key_Home, End: Qt.Key_End, PageUp: Qt.Key_PageUp, PageDown: Qt.Key_PageDown,
        };
        const key = named[name] ?? name.toUpperCase().charCodeAt(0);
        const text = ctrl || named[name] !== undefined ? "" : name;
        if (mode === "normal")
            normalKey(key, text, ctrl);
        else if (!insertKey(key, text, ctrl) && text)
            input.insert(input.cursorPosition, text);
    }

    readonly property var hints: {
        if (mode === "insert")
            return [
                ["insert", [["Esc or Ctrl+Enter", "done, keeping the filter"], ["Ctrl+j/k", "move"]]],
                ["search", [["Enter", "search YouTube"], ["Ctrl+Enter", "also does when nothing matches"]]],
                ["", []],
            ];
        if (channels)
            return [
                ["move", [["j/k", "down/up"], ["g/G", "top/bottom"], ["i or /", "filter"]]],
                ["channel", [["Enter or l", "open"], ["s", "unsubscribe"]]],
                ["go to", [["h or Esc", "back"], ["q", "quit"]]],
            ];
        const it = list.currentItem;
        return [
            ["move", [["j/k", "down/up"], ["g/G", "top/bottom"], ["Ctrl+d/u", "page"],
                      ["i or /", "search"]]],
            ["video", [["Enter or l", "play"],
                       ["w", it && it.later ? "remove from watch later" : "watch later"],
                       ["m", it && it.watched ? "mark unwatched" : "mark watched"],
                       ["c", "channel"],
                       ["s", it && it.subscribed ? "unsubscribe" : "subscribe"],
                       ["y", "copy link"]]],
            ["go to", [["W", "watch later list"], ["S", "subscriptions"], ["r", "refresh feed"],
                       ["h or Esc", "back"], ["q", "quit"]]],
        ];
    }

    readonly property string emptyText: {
        if (rows.count)
            return "";
        if (loading)
            return kind === "search" ? "Searching YouTube…" : "Loading…";
        if (error)
            return "⚠ " + error;
        if (filter)
            return mode === "insert"
                ? `Nothing matches. Enter searches YouTube for “${filter.trim()}”`
                : `Nothing matches “${filter}”`;
        return {
            feed: "No videos yet. Subscribe to channels with s.",
            later: "Nothing saved. Press w on a video to watch it later.",
            subs: "No subscriptions yet.",
            search: "No results.",
            channel: "No videos.",
            prompt: "Type to search YouTube, then press Enter.",
        }[kind] || "";
    }

    // --- backend -------------------------------------------------------------

    function request(msg, callback) {
        msg.req = nextReq++;
        if (msg.cmd !== "list") {
            pendingActions++;
            const then = callback;
            callback = reply => {
                pendingActions--;
                then(reply);
                if (quitting && !pendingActions)
                    Qt.quit();
            };
        }
        callbacks[msg.req] = callback;
        backend.write(JSON.stringify(msg) + "\n");
    }

    function received(line) {
        let msg;
        try {
            msg = JSON.parse(line);
        } catch (e) {
            return console.warn("tube serve:", line);
        }
        if (msg.event === "thumb") {
            pendingThumbs[msg.id] = msg.path;
            if (!thumbTimer.running)
                thumbTimer.start();
        } else if (msg.event === "feed-updated") {
            for (const v of stack)
                if (v.kind === "feed")
                    load(v);
        } else if (callbacks[msg.req]) {
            const callback = callbacks[msg.req];
            delete callbacks[msg.req];
            callback(msg);
        }
    }

    function start() {
        const opts = JSON.parse(Quickshell.env("TUBE_START") || "{}");
        stack = [makeView("feed")];
        load(stack[0]);
        if (opts.search !== undefined) {
            const v = opts.search ? makeView("search", {query: opts.search}) : makeView("prompt");
            stack.push(v);
            load(v);
        }
        show(true);
        if (opts.search === "")
            enterInsert();
    }

    Process {
        id: backend
        command: JSON.parse(Quickshell.env("TUBE_BACKEND") || '["tube", "serve"]')
        running: true
        stdinEnabled: true
        stdout: SplitParser {
            onRead: line => root.received(line)
        }
        stderr: SplitParser {
            onRead: line => console.warn("tube serve:", line)
        }
        onStarted: root.start()
        onExited: code => {
            if (root.quitting)
                Qt.quit();
            else
                root.say(`⚠ the backend exited (${code}); see quickshell's log`);
        }
    }

    Timer {  // thumbnails arrive one by one; apply them in batches
        id: thumbTimer
        interval: 100
        onTriggered: root.applyThumbs()
    }

    Timer {
        id: statusTimer
        interval: 5000
        onTriggered: root.status = ""
    }

    Timer {  // in case the backend never answers
        id: quitAnyway
        interval: 3000
        onTriggered: Qt.quit()
    }

    IpcHandler {
        target: "tube"

        function key(name: string): void {
            root.press(name);
        }
        function type(text: string): void {
            if (root.mode === "insert")
                input.insert(input.cursorPosition, text);
        }
        function info(): string {
            const it = root.current();
            return JSON.stringify({
                mode: root.mode, title: root.title, filter: root.filter, rows: rows.count,
                index: list.currentIndex, key: it ? it.key : null, status: root.status,
                depth: root.stack.length,
            });
        }
        function quit(): void {
            root.quit();
        }
    }

    // --- window --------------------------------------------------------------

    PanelWindow {
        id: window

        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "tube"
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
        exclusionMode: ExclusionMode.Ignore
        color: "transparent"
        implicitWidth: 1040
        implicitHeight: card.height

        Rectangle {
            id: card
            width: parent.width
            height: content.implicitHeight + 32
            radius: 14
            color: Theme.base
            border.width: 2
            border.color: Theme.surface1

            Item {  // takes the keys in normal mode
                id: keys
                focus: true
                Keys.onPressed: event => {
                    event.accepted = root.normalKey(event.key, event.text,
                                                    event.modifiers & Qt.ControlModifier);
                }
            }

            Column {
                id: content
                x: 16
                y: 16
                width: parent.width - 32
                spacing: 12

                Rectangle {
                    id: bar
                    width: parent.width
                    height: 44
                    radius: 10
                    color: Theme.surface0

                    Text {
                        id: prompt
                        anchors.left: parent.left
                        anchors.leftMargin: 14
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.min(implicitWidth, bar.width * 0.4)
                        text: root.title
                        color: Theme.mauve
                        font.family: Theme.sans
                        font.pointSize: 12
                        elide: Text.ElideRight
                    }

                    TextInput {
                        id: input
                        visible: root.mode === "insert"
                        anchors.left: prompt.right
                        anchors.leftMargin: 12
                        anchors.right: badge.left
                        anchors.rightMargin: 12
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.text
                        selectionColor: Theme.surface1
                        selectedTextColor: Theme.text
                        font.family: Theme.sans
                        font.pointSize: 12
                        clip: true
                        onTextChanged: root.typed()
                        Keys.onPressed: event => {
                            event.accepted = root.insertKey(event.key, event.text,
                                                            event.modifiers & Qt.ControlModifier);
                        }

                        Text {
                            visible: !input.text
                            text: "filter, or type to search YouTube"
                            color: Theme.subtext
                            font: input.font
                        }
                    }

                    Text {  // normal mode: the filter in place of the text box
                        visible: root.mode === "normal"
                        anchors.left: prompt.right
                        anchors.leftMargin: 12
                        anchors.right: badge.left
                        anchors.rightMargin: 12
                        anchors.verticalCenter: parent.verticalCenter
                        text: root.filter ? "/" + root.filter
                            : root.channels ? "press i to filter" : "press i to search"
                        color: root.filter ? Theme.text : Theme.subtext
                        font.family: Theme.sans
                        font.pointSize: 12
                        elide: Text.ElideRight
                    }

                    Rectangle {  // vim-style mode badge
                        id: badge
                        anchors.right: parent.right
                        anchors.rightMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        width: badgeText.implicitWidth + 16
                        height: badgeText.implicitHeight + 4
                        radius: 6
                        color: root.mode === "insert" ? Theme.green : Theme.mauve
                        Text {
                            id: badgeText
                            anchors.centerIn: parent
                            text: root.mode.toUpperCase()
                            color: Theme.base
                            font.family: Theme.mono
                            font.pointSize: 9
                            font.bold: true
                        }
                    }
                }

                ListView {
                    id: list
                    width: parent.width
                    height: 8 * 102 + 7 * spacing
                    spacing: 2
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    model: rows
                    delegate: root.channels ? channelRow : videoRow
                    onCurrentIndexChanged: {
                        if (!root.populating)
                            root.remember();
                    }

                    Text {
                        anchors.centerIn: parent
                        width: parent.width - 80
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        text: root.emptyText
                        color: Theme.subtext
                        font.family: Theme.sans
                        font.pointSize: 12
                    }
                }

                Column {
                    width: parent.width
                    spacing: 8

                    Text {
                        width: parent.width
                        height: 18
                        leftPadding: 4
                        text: root.status
                        color: Theme.text
                        font.family: Theme.sans
                        font.pointSize: 10
                        font.bold: true
                        elide: Text.ElideRight
                    }

                    KeyHints {
                        x: 4
                        groups: root.hints
                    }
                }
            }
        }

        Component.onCompleted: keys.forceActiveFocus()
    }

    Component {
        id: videoRow
        VideoRow {
            width: list.width
            current: ListView.isCurrentItem
            onClicked: list.currentIndex = index
            onActivated: {
                list.currentIndex = index;
                root.accept();
            }
        }
    }

    Component {
        id: channelRow
        ChannelRow {
            width: list.width
            current: ListView.isCurrentItem
            onClicked: list.currentIndex = index
            onActivated: {
                list.currentIndex = index;
                root.accept();
            }
        }
    }
}
