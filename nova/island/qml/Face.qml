import QtQuick

// One state's content inside the pill: fades and scales in when its state starts, fades out
// when it ends. While inactive it keeps its last width, so content that is fading out (or
// riding a collapsing pill) never re-wraps or jumps. Render-thread animators only.
Item {
    id: face

    property bool active: false
    property real targetWidth: 0  // the width to lay out at while active
    property color targetAccent: "#0A84FF"
    property color accent: "#0A84FF"  // the state's colour, kept while fading out
    signal entered()

    function latch() {
        if (!active)
            return
        width = targetWidth
        accent = targetAccent
    }

    width: 0
    opacity: active ? 1 : 0
    scale: active ? 1 : 0.94
    visible: active || opacity > 0  // an animator writes opacity only when it ends
    enabled: active
    onTargetWidthChanged: latch()
    onTargetAccentChanged: latch()
    onActiveChanged: {
        latch()
        if (active)
            entered()
    }

    Behavior on opacity { OpacityAnimator { duration: face.active ? 220 : 110; easing.type: Easing.OutCubic } }
    Behavior on scale { ScaleAnimator { duration: 260; easing.type: Easing.OutBack } }
}
