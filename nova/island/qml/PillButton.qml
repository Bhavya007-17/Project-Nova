import QtQuick

// A small pill button. Clicks work without focus: the island never takes it.
Rectangle {
    id: button

    property string text: ""
    property color fill: "#2C2C2E"
    property color textColor: "#FFFFFF"
    signal clicked()

    implicitWidth: Math.max(92, label.implicitWidth + 36)
    implicitHeight: 32
    radius: height / 2
    color: area.pressed ? Qt.darker(fill, 1.2) : area.containsMouse ? Qt.lighter(fill, 1.18) : fill
    antialiasing: true

    Text {
        id: label
        anchors.centerIn: parent
        text: button.text
        color: button.textColor
        textFormat: Text.PlainText
        font.family: "Segoe UI Variable Text"
        font.weight: Font.DemiBold
        font.pixelSize: 14
    }

    MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: button.clicked()
    }
}
