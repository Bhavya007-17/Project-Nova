pragma ComponentBehavior: Bound

import QtQuick

// "Acting": the plan's steps, e.g. Chrome → Canvas → Today. Finished steps dim, the running
// one breathes, later ones wait faintly. Keeps the running end in view.
Item {
    id: line

    property var steps: []
    property int active: 0
    property bool running: false

    implicitWidth: row.implicitWidth
    implicitHeight: 20
    clip: true

    Row {
        id: row
        x: Math.min(0, line.width - implicitWidth)
        anchors.verticalCenter: parent.verticalCenter
        spacing: 7

        Repeater {
            model: line.steps

            Row {
                id: item
                required property string modelData
                required property int index
                readonly property bool current: index === line.active

                spacing: 7

                Text {
                    visible: item.index > 0
                    anchors.verticalCenter: parent.verticalCenter
                    text: "→"
                    color: "#FFFFFF"
                    opacity: 0.35
                    font.family: "Segoe UI Variable Text"
                    font.pixelSize: 14
                }

                Text {
                    id: label
                    anchors.verticalCenter: parent.verticalCenter
                    text: item.modelData
                    color: "#FFFFFF"
                    opacity: item.index < line.active ? 0.55 : item.current ? 1 : 0.32
                    textFormat: Text.PlainText
                    font.family: "Segoe UI Variable Text"
                    font.weight: item.current ? Font.DemiBold : Font.Medium
                    font.pixelSize: 15

                    SequentialAnimation {
                        loops: Animation.Infinite
                        running: item.current && line.running

                        OpacityAnimator { target: label; from: 1; to: 0.55; duration: 650; easing.type: Easing.InOutSine }
                        OpacityAnimator { target: label; from: 0.55; to: 1; duration: 650; easing.type: Easing.InOutSine }
                    }
                }
            }
        }
    }

    Rectangle {
        visible: row.implicitWidth > line.width
        width: 26
        height: parent.height
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "#FF000000" }
            GradientStop { position: 1.0; color: "#00000000" }
        }
    }
}
