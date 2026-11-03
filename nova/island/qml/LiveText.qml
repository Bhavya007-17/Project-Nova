import QtQuick

// One line of words. Live words (partials) show dimmed and the newest ones fade in; a final
// settles to full white. A line longer than the space keeps its end in view and fades out on
// the left. `fresh` = where the newly arrived words start (worked out in Python, view.py).
Item {
    id: live

    property string text: ""
    property int fresh: 0
    property bool final: false
    property bool windowed: false
    property string placeholder: ""
    property color color: "#FFFFFF"
    property int pixelSize: 15

    readonly property string older: (windowed ? "… " : "") + text.slice(0, fresh)
    readonly property string newer: text.slice(fresh)
    readonly property bool overflows: row.implicitWidth > width

    implicitWidth: text ? row.implicitWidth : hint.implicitWidth
    implicitHeight: Math.ceil(olderText.implicitHeight)
    clip: true

    Row {
        id: row
        x: Math.min(0, live.width - implicitWidth)  // keep the end of a long line in view
        anchors.verticalCenter: parent.verticalCenter
        opacity: live.final ? 1 : 0.62
        Behavior on opacity { OpacityAnimator { duration: 220; easing.type: Easing.OutCubic } }

        Text {
            id: olderText
            text: live.older
            color: live.color
            textFormat: Text.PlainText
            font.family: "Segoe UI Variable Text"
            font.weight: Font.Medium
            font.pixelSize: live.pixelSize
        }

        Text {
            id: newerText
            text: live.newer
            color: live.color
            textFormat: Text.PlainText
            font: olderText.font
            onTextChanged: if (newerText.text) arrive.restart()

            OpacityAnimator { id: arrive; target: newerText; from: 0; to: 1; duration: 200 }
        }
    }

    Text {
        id: hint
        anchors.verticalCenter: parent.verticalCenter
        width: live.width
        visible: !live.text
        text: live.placeholder
        color: live.color
        opacity: 0.5
        elide: Text.ElideRight
        textFormat: Text.PlainText
        font: olderText.font
    }

    Rectangle {  // left fade over the start of a long line (the pill is true black)
        visible: live.overflows
        width: 26
        height: parent.height
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "#FF000000" }
            GradientStop { position: 1.0; color: "#00000000" }
        }
    }
}
