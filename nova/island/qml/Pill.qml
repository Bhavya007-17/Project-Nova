import QtQuick

// The true-black island. Width, height and the drop-down move on a stiff, slightly bouncy
// spring; radius = height / 2. Qt steps SpringAnimation every 16 ms (v += spring * diff -
// damping * v): spring 5 / damping 0.35 overshoots ~5 % and settles in ~0.3 s.
Rectangle {
    id: pill

    property real targetWidth: 112
    property real targetHeight: 30
    property real targetY: -46
    readonly property bool morphing: widthSpring.running || heightSpring.running || ySpring.running

    width: targetWidth
    height: targetHeight
    y: targetY
    radius: height / 2
    color: "#000000"
    border.width: 0.5  // one physical pixel at 200 %: the pill still reads on dark title bars
    border.color: Qt.rgba(1, 1, 1, 0.10)
    antialiasing: true
    clip: true

    Behavior on width { SpringAnimation { id: widthSpring; spring: 5; damping: 0.35; epsilon: 0.25 } }
    Behavior on height { SpringAnimation { id: heightSpring; spring: 5; damping: 0.35; epsilon: 0.25 } }
    Behavior on y { SpringAnimation { id: ySpring; spring: 5; damping: 0.35; epsilon: 0.25 } }
}
