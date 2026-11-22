import QtQuick

// One subscription: the channel's name and when it last uploaded.
Item {
    id: row

    required property int index
    required property string title
    required property string age
    property bool current: false

    signal clicked
    signal activated

    implicitHeight: 44

    Rectangle {
        anchors.fill: parent
        radius: 10
        color: row.current ? Theme.surface0 : "transparent"
    }

    Text {
        anchors.left: parent.left
        anchors.leftMargin: 14
        anchors.right: ageText.left
        anchors.rightMargin: 14
        anchors.verticalCenter: parent.verticalCenter
        text: row.title
        color: Theme.text
        font.family: Theme.sans
        font.pointSize: 12
        elide: Text.ElideRight
    }

    Text {
        id: ageText
        anchors.right: parent.right
        anchors.rightMargin: 14
        anchors.verticalCenter: parent.verticalCenter
        text: row.age
        color: Theme.subtext
        font.family: Theme.sans
        font.pointSize: 9.5
    }

    MouseArea {
        anchors.fill: parent
        onClicked: row.clicked()
        onDoubleClicked: row.activated()
    }
}
