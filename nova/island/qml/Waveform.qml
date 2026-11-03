pragma ComponentBehavior: Bound

import QtQuick

// The mic level as five bars rippling out from the middle: the newest level drives the centre
// bar, older ones the outer bars. ~30 updates a second while listening; nothing runs otherwise.
Item {
    id: wave

    property real level: 0
    property color color: "#0A84FF"
    property bool shown: true
    property var history: [0, 0, 0]
    readonly property real barWidth: 3
    readonly property real maxHeight: 18

    implicitWidth: 5 * barWidth + 4 * 3
    implicitHeight: maxHeight
    opacity: shown ? 1 : 0
    visible: shown || opacity > 0
    Behavior on opacity { OpacityAnimator { duration: 160 } }

    onLevelChanged: history = [level, history[0], history[1]]
    onShownChanged: if (!shown) history = [0, 0, 0]

    Row {
        anchors.centerIn: parent
        spacing: 3

        Repeater {
            model: 5

            Rectangle {
                required property int index
                readonly property int age: Math.abs(index - 2)

                anchors.verticalCenter: parent.verticalCenter
                width: wave.barWidth
                height: Math.max(wave.barWidth, wave.maxHeight * [1.0, 0.78, 0.56][age] * wave.history[age])
                radius: width / 2
                color: wave.color  // no height animation: frames only when a level arrives (~30/s), not 60/s
            }
        }
    }
}
