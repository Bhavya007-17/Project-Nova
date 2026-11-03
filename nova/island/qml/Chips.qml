pragma ComponentBehavior: Bound

import QtQuick

// Background jobs as small pills beside the island: "Claude · HW 5 · working…" -> "done ✓".
// Static while they wait (jobs can run for minutes). A click asks Nova to focus that window.
Column {
    id: chips

    property var model: []
    signal clicked(string id)

    spacing: 6

    Repeater {
        model: chips.model

        Rectangle {
            id: chip
            required property var modelData

            readonly property string status: modelData.status
            readonly property color tint: status === "done" ? "#30D158" : status === "failed" ? "#FF453A" : "#0A84FF"

            width: Math.min(230, content.implicitWidth + 26)
            height: 30
            radius: 15
            color: "#000000"
            border.width: 0.5
            border.color: area.containsMouse ? Qt.rgba(1, 1, 1, 0.30) : Qt.rgba(1, 1, 1, 0.14)
            antialiasing: true

            Row {
                id: content
                anchors.left: parent.left
                anchors.leftMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                spacing: 7

                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: chip.status === "working"
                    width: 7
                    height: 7
                    radius: 3.5
                    color: chip.tint
                }

                Text {
                    width: Math.min(implicitWidth, 230 - 26 - 14)
                    anchors.verticalCenter: parent.verticalCenter
                    text: chip.modelData.text + (chip.status === "done" ? " · done" : chip.status === "failed" ? " · failed" : " · working…")
                    color: "#FFFFFF"
                    opacity: chip.status === "working" ? 0.85 : 1
                    elide: Text.ElideMiddle
                    textFormat: Text.PlainText
                    font.family: "Segoe UI Variable Small"
                    font.weight: Font.DemiBold
                    font.pixelSize: 13
                }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: chip.status !== "working"
                    text: chip.status === "done" ? "\uE73E" : "\uE711"
                    color: chip.tint
                    font.family: "Segoe Fluent Icons"
                    font.pixelSize: 12
                    font.bold: true
                }
            }

            MouseArea {
                id: area
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: chips.clicked(chip.modelData.id)
            }
        }
    }
}
