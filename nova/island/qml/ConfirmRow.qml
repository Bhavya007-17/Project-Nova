import QtQuick

// Confirm: the exact action and [No] [Yes]. Also answered by voice ("yes" / "no"), Enter / Esc
// (read by the keyboard hook: the island never takes focus) and a 15 s timeout (= no).
Item {
    id: confirm

    property string question: ""
    property color accent: "#FFB340"
    signal answered(string what)

    implicitWidth: Math.max(ask.implicitWidth, buttons.implicitWidth)
    implicitHeight: ask.implicitHeight + 14 + buttons.implicitHeight

    Item {
        id: ask
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(implicitWidth, confirm.width)
        implicitWidth: mark.width + 9 + askText.implicitWidth
        implicitHeight: askText.implicitHeight

        Text {
            id: mark
            anchors.top: parent.top
            anchors.topMargin: 3
            text: "\uE7BA"  // warning
            color: confirm.accent
            font.family: "Segoe Fluent Icons"
            font.pixelSize: 15
        }

        Text {
            id: askText
            anchors.left: mark.right
            anchors.leftMargin: 9
            anchors.right: parent.right
            text: confirm.question
            color: "#FFFFFF"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            font.family: "Segoe UI Variable Text"
            font.weight: Font.DemiBold
            font.pixelSize: 15
        }
    }

    Row {
        id: buttons
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        spacing: 10

        PillButton { text: "No"; onClicked: confirm.answered("no") }
        PillButton { text: "Yes"; fill: confirm.accent; textColor: "#000000"; onClicked: confirm.answered("yes") }
    }
}
