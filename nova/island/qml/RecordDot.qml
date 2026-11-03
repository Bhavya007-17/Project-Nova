import QtQuick

// Teaching indicator: a red recording dot with a soft halo. Static: teaching can last minutes.
Item {
    id: dot

    property color color: "#FF453A"
    property bool shown: true

    implicitWidth: 20
    implicitHeight: 20
    opacity: shown ? 1 : 0
    visible: shown || opacity > 0
    Behavior on opacity { OpacityAnimator { duration: 160 } }

    Rectangle {
        anchors.centerIn: parent
        width: 18
        height: 18
        radius: 9
        color: dot.color
        opacity: 0.22
    }

    Rectangle {
        anchors.centerIn: parent
        width: 10
        height: 10
        radius: 5
        color: dot.color
    }
}
