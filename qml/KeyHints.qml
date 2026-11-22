import QtQuick

// Rows of key hints: [[label, [[key, description], ...]], ...]
Column {
    id: hints

    property var groups: []

    spacing: 6

    Repeater {
        model: hints.groups

        Row {
            id: group
            required property var modelData
            height: 20  // also for an empty row, so the window keeps its size
            spacing: 14

            Text {
                width: 64
                text: group.modelData[0]
                color: Theme.mauve
                font.family: Theme.mono
                font.pointSize: 9
                anchors.verticalCenter: parent.verticalCenter
            }

            Repeater {
                model: group.modelData[1]

                Row {
                    id: hint
                    required property var modelData
                    spacing: 6

                    Rectangle {
                        width: keyText.implicitWidth + 10
                        height: keyText.implicitHeight + 4
                        radius: 4
                        color: Theme.surface0
                        Text {
                            id: keyText
                            anchors.centerIn: parent
                            text: hint.modelData[0]
                            color: Theme.text
                            font.family: Theme.mono
                            font.pointSize: 9
                        }
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: hint.modelData[1]
                        color: Theme.subtext
                        font.family: Theme.sans
                        font.pointSize: 9
                    }
                }
            }
        }
    }
}
