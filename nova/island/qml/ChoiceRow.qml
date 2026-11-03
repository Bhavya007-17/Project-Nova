pragma ComponentBehavior: Bound

import QtQuick

// Clarify: a question and up to 4 numbered choices. Answered by voice ("the second one",
// "chrome"), a click, or the number keys (read by the keyboard hook).
Item {
    id: clarify

    property string question: ""
    property var choices: []
    property color accent: "#0A84FF"
    signal picked(int index)

    readonly property int columns: choices.length > 2 ? 2 : Math.max(1, choices.length)
    readonly property real cellWidth: Math.min(220, (width - (columns - 1) * 8) / columns)

    implicitWidth: Math.max(ask.implicitWidth, columns * 190 + (columns - 1) * 8)
    implicitHeight: ask.implicitHeight + 12 + grid.implicitHeight

    Text {
        id: ask
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(implicitWidth, clarify.width)
        text: clarify.question
        color: "#FFFFFF"
        horizontalAlignment: Text.AlignHCenter
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
        font.family: "Segoe UI Variable Text"
        font.weight: Font.DemiBold
        font.pixelSize: 15
    }

    Grid {
        id: grid
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        columns: clarify.columns
        spacing: 8

        Repeater {
            model: clarify.choices

            Rectangle {
                id: choice
                required property string modelData
                required property int index

                width: clarify.cellWidth
                height: 30
                radius: 15
                color: area.pressed ? "#3A3A3C" : area.containsMouse ? "#2C2C2E" : "#1C1C1E"

                Rectangle {
                    id: number
                    anchors.left: parent.left
                    anchors.leftMargin: 6
                    anchors.verticalCenter: parent.verticalCenter
                    width: 18
                    height: 18
                    radius: 9
                    color: clarify.accent

                    Text {
                        anchors.centerIn: parent
                        text: choice.index + 1
                        color: "#000000"
                        font.family: "Segoe UI Variable Text"
                        font.weight: Font.Bold
                        font.pixelSize: 11
                    }
                }

                Text {
                    anchors.left: number.right
                    anchors.leftMargin: 8
                    anchors.right: parent.right
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    text: choice.modelData
                    color: "#FFFFFF"
                    elide: Text.ElideRight
                    textFormat: Text.PlainText
                    font.family: "Segoe UI Variable Text"
                    font.weight: Font.Medium
                    font.pixelSize: 14
                }

                MouseArea {
                    id: area
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: clarify.picked(choice.index)
                }
            }
        }
    }
}
