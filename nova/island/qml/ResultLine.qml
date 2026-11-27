import QtQuick

// ✓ done · ⚡ done by a learned skill · ✗ failed — and the short result in plain words.
Item {
    id: line

    property string icon: "ok"  // ok | skill | fail
    property string text: ""
    property color accent: "#30D158"
    property int maximumLineCount: 1

    function pop() { popIn.restart() }

    implicitWidth: badge.width + 10 + label.implicitWidth
    implicitHeight: Math.max(badge.height, label.implicitHeight)

    Rectangle {
        id: badge
        anchors.verticalCenter: parent.verticalCenter
        width: 22
        height: 22
        radius: 11
        color: line.accent

        Text {
            anchors.centerIn: parent
            text: line.icon === "skill" ? (Qt.platform.os === "osx" ? "⚡" : "\uE945") : line.icon === "fail" ? (Qt.platform.os === "osx" ? "✕" : "\uE711") : (Qt.platform.os === "osx" ? "✓" : "\uE73E")
            color: "#000000"
            font.family: Qt.platform.os === "osx" ? Qt.application.font.family : "Segoe Fluent Icons"
            font.pixelSize: line.icon === "skill" ? 13 : 11
            font.bold: true
        }

        ScaleAnimator { id: popIn; target: badge; from: 0.45; to: 1; duration: 380; easing.type: Easing.OutBack; easing.overshoot: 2.2 }
    }

    Text {
        id: label
        anchors.left: badge.right
        anchors.leftMargin: 10
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: line.text
        color: "#FFFFFF"
        textFormat: Text.PlainText
        wrapMode: line.maximumLineCount > 1 ? Text.Wrap : Text.NoWrap
        maximumLineCount: line.maximumLineCount
        elide: Text.ElideRight
        font.family: Qt.platform.os === "osx" ? Qt.application.font.family : "Segoe UI Variable Text"
        font.weight: Font.DemiBold
        font.pixelSize: 15
    }
}
