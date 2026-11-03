import QtQuick

// "Thinking": a small orb whose gradient turns, with a soft breathing glow. Cheap: three
// rounded rectangles and render-thread animators that run only while it shows.
Item {
    id: orb

    property color color: "#0A84FF"
    property bool shown: true
    property bool running: false

    implicitWidth: 20
    implicitHeight: 20
    opacity: shown ? 1 : 0
    visible: shown || opacity > 0
    Behavior on opacity { OpacityAnimator { duration: 160 } }

    Rectangle {
        id: glow
        anchors.centerIn: parent
        width: parent.width + 8
        height: width
        radius: width / 2
        color: orb.color
        opacity: 0.2
    }

    Rectangle {
        id: ball
        anchors.fill: parent
        radius: width / 2
        antialiasing: true
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#64D2FF" }
            GradientStop { position: 0.5; color: orb.color }
            GradientStop { position: 1.0; color: "#BF5AF2" }
        }
    }

    Rectangle {  // highlight
        x: parent.width * 0.2
        y: parent.height * 0.14
        width: parent.width * 0.38
        height: width
        radius: width / 2
        color: "#FFFFFF"
        opacity: 0.32
    }

    RotationAnimator {
        target: ball
        from: 0
        to: 360
        duration: 1400
        loops: Animation.Infinite
        running: orb.running
    }

    SequentialAnimation {
        loops: Animation.Infinite
        running: orb.running

        OpacityAnimator { target: glow; from: 0.12; to: 0.4; duration: 700; easing.type: Easing.InOutSine }
        OpacityAnimator { target: glow; from: 0.4; to: 0.12; duration: 700; easing.type: Easing.InOutSine }
    }
}
