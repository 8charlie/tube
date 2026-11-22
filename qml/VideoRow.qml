import QtQuick
import Quickshell.Widgets

// One video: a 16:9 thumbnail with its length and progress, then the title
// and a line of details. Watched videos are dimmed.
Item {
    id: row

    required property int index
    required property string title
    required property string channel
    required property string age
    required property string length
    required property string position
    required property real progress
    required property bool watched
    required property bool later
    required property bool subscribed
    required property string thumb
    property bool current: false

    signal clicked
    signal activated

    implicitHeight: 102

    Rectangle {
        anchors.fill: parent
        radius: 10
        color: row.current ? Theme.surface0 : "transparent"
    }

    ClippingRectangle {
        id: thumbnail
        x: 6
        y: 6
        width: 160
        height: 90
        radius: 7
        color: Theme.mantle
        opacity: row.watched ? 0.4 : 1

        Image {
            anchors.fill: parent
            source: row.thumb ? "file://" + row.thumb : ""
            sourceSize: Qt.size(320, 180)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
        }

        Rectangle {  // saved for later
            visible: row.later
            x: 5
            y: 5
            width: 20
            height: 20
            radius: 10
            color: Theme.accent
            Text {
                anchors.centerIn: parent
                text: "★"
                color: Theme.base
                font.pixelSize: 12
            }
        }

        Rectangle {
            visible: row.length !== ""
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.margins: 5
            anchors.bottomMargin: row.progress > 0 ? 8 : 5
            width: lengthText.implicitWidth + 8
            height: lengthText.implicitHeight + 2
            radius: 4
            color: "#cc000000"
            Text {
                id: lengthText
                anchors.centerIn: parent
                text: row.length
                color: "white"
                font.family: Theme.sans
                font.pointSize: 8
                font.weight: Font.Medium
            }
        }

        Rectangle {  // how far you got
            visible: row.progress > 0
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 4
            color: "#80000000"
            Rectangle {
                width: parent.width * row.progress
                height: parent.height
                color: Theme.accent
            }
        }
    }

    Column {
        anchors.left: thumbnail.right
        anchors.leftMargin: 14
        anchors.right: parent.right
        anchors.rightMargin: 12
        anchors.verticalCenter: parent.verticalCenter
        spacing: 4
        opacity: row.watched ? 0.45 : 1

        Text {
            width: parent.width
            text: row.title
            color: Theme.text
            font.family: Theme.sans
            font.pointSize: 12
            font.weight: Font.Medium
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
        }

        Text {
            width: parent.width
            text: {
                const meta = [row.channel, row.age];
                if (row.position && row.length)
                    meta.push(row.position + " / " + row.length);
                else if (row.position)
                    meta.push("resume at " + row.position);
                return meta.filter(m => m).join("  ·  ");
            }
            color: Theme.subtext
            font.family: Theme.sans
            font.pointSize: 9.5
            elide: Text.ElideRight
        }
    }

    MouseArea {
        anchors.fill: parent
        onClicked: row.clicked()
        onDoubleClicked: row.activated()
    }
}
