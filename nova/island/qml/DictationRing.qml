import QtQuick

// Dictating indicator: an orange ring. Static: dictation can last ten minutes.
Item {
    id: ring

    property color color: "#FF9F0A"
    property bool shown: true

    implicitWidth: 20
    implicitHeight: 20
    opacity: shown ? 1 : 0
    visible: shown || opacity > 0
    Behavior on opacity { OpacityAnimator { duration: 160 } }

    Rectangle {
        anchors.centerIn: parent
        width: 16
        height: 16
        radius: 8
        color: "transparent"
        border.width: 2.5
        border.color: ring.color
        antialiasing: true
    }
}
