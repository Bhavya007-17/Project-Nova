import QtQuick

// Nova's island (SPEC §6): a true-black pill at the top of the screen. Compact (~44 tall) for
// live words, steps and results; expanded (≤ 560 × 160) for confirm, clarify and errors; chips
// for background jobs beside it; pinned (Ctrl + Copilot), a small status pill between commands
// that can be dragged anywhere. Everything shown comes from `island` (nova/island/bridge.py).
// Hidden = the window is hidden: nothing here runs then.
Item {
    id: root

    required property var island

    readonly property string mode: island.state
    readonly property bool open: island.open
    property color accent: island.accent  // kept while the pill collapses (hidden has no accent of its own)
    Connections {
        target: root.island
        function onAccentChanged() { if (root.island.open) root.accent = root.island.accent }
    }
    readonly property bool hasChips: island.chips.length > 0
    property string lastMode: "listening"  // what the pill showed last: kept while it collapses
    onModeChanged: if (mode !== "hidden") lastMode = mode

    readonly property real topMargin: 8
    readonly property real compactHeight: 44
    readonly property real padLeft: 11  // the 24-px indicator box sits in the left end's curve
    readonly property real padRight: 20
    readonly property real lead: 24
    readonly property real expandedPadX: 34
    readonly property real expandedPadY: 18
    readonly property real idleHeight: 34

    readonly property bool liveMode: ["listening", "thinking", "dictating", "teaching", "passthrough"].indexOf(mode) >= 0
    readonly property Item face: liveMode ? liveFace
        : mode === "acting" ? actingFace
        : mode === "done" ? doneFace
        : mode === "confirm" ? confirmFace
        : mode === "clarify" ? clarifyFace
        : mode === "error" ? errorFace
        : mode === "idle" ? idleFace
        : null
    readonly property real targetWidth: !open || !face ? 112
        : island.expanded ? clamp(Math.ceil(face.implicitWidth) + 2 + 2 * expandedPadX, 380, 560)
        : mode === "idle" ? Math.ceil(face.implicitWidth) + 2 + 2 * 16
        : clamp(Math.ceil(face.implicitWidth) + 2 + padLeft + padRight, 176, 420)  // +2: text that just fits never elides
    readonly property real targetHeight: !open || !face ? 30
        : island.expanded ? Math.min(160, face.implicitHeight + 2 * expandedPadY)
        : mode === "idle" ? idleHeight
        : compactHeight

    function clamp(value, low, high) { return Math.max(low, Math.min(high, value)) }

    // A pill dragged near a screen edge slides inward as it grows, so it never runs off the screen.
    readonly property real windowX: Window.window ? Window.window.x : 0
    readonly property real shift: {
        const centre = windowX + width / 2
        const half = targetWidth / 2 + 8
        return clamp(centre, Screen.virtualX + half, Screen.virtualX + Screen.width - half) - centre
    }
    property real nudge: shift  // `shift`, on the island spring
    Behavior on nudge { SpringAnimation { id: shiftSpring; spring: 5; damping: 0.35; epsilon: 0.25 } }

    // --- the pill ---------------------------------------------------------------------------

    Pill {
        id: pill
        x: (root.width - width) / 2 + root.nudge
        targetWidth: root.targetWidth
        targetHeight: root.targetHeight
        targetY: root.open ? root.topMargin : -root.targetHeight - 16  // closed: tucked above the top edge
        opacity: root.open ? 1 : 0
        Behavior on opacity { OpacityAnimator { duration: root.open ? 120 : 260 } }

        // Drag the pill (whenever it takes clicks: pinned, or showing buttons) to move the island;
        // where it is dropped becomes its home. Double-click: back to the top centre. Buttons sit
        // above this, so they still get their clicks. Moves the window itself: no Python per move.
        MouseArea {
            id: dragArea
            property point grabbed
            property point origin
            property bool moving: false

            anchors.fill: parent
            enabled: root.island.interactive
            cursorShape: moving ? Qt.SizeAllCursor : Qt.ArrowCursor
            onPressed: (mouse) => {
                grabbed = mapToGlobal(mouse.x, mouse.y)
                origin = Qt.point(root.Window.window.x, root.Window.window.y)
                moving = false
            }
            onPositionChanged: (mouse) => {
                const at = mapToGlobal(mouse.x, mouse.y)
                const dx = at.x - grabbed.x
                const dy = at.y - grabbed.y
                if (!moving && Math.abs(dx) + Math.abs(dy) < 5)
                    return  // a click or a double-click, not a drag
                moving = true
                root.Window.window.x = origin.x + dx
                root.Window.window.y = origin.y + dy
            }
            onReleased: if (moving) { moving = false; root.island.dropped() }
            onCanceled: if (moving) { moving = false; root.island.dropped() }
            onDoubleClicked: root.island.resetHome()
        }

        // pinned and nothing going on: a status dot (blue = always-listening on, grey = off) + "Nova"
        Face {
            id: idleFace
            active: root.mode === "idle"
            targetAccent: root.accent
            targetWidth: root.targetWidth - 2 * 16
            x: (pill.width - width) / 2
            y: (pill.height - height) / 2
            height: 20
            implicitWidth: idleRow.implicitWidth

            Row {
                id: idleRow
                anchors.centerIn: parent
                spacing: 8

                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 8
                    height: 8
                    radius: 4
                    color: root.island.always ? "#0A84FF" : "#8E8E93"
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Nova"
                    color: "#FFFFFF"
                    opacity: 0.85
                    font.family: "Segoe UI Variable Text"
                    font.weight: Font.DemiBold
                    font.pixelSize: 14
                }
            }
        }

        // listening · thinking · dictating · teaching · passthrough: an indicator + one line
        Face {
            id: liveFace
            readonly property bool noted: root.lastMode === "teaching" || root.lastMode === "passthrough"

            active: root.liveMode
            targetAccent: root.accent
            targetWidth: root.targetWidth - root.padLeft - root.padRight
            x: (pill.width - width - root.padLeft - root.padRight) / 2 + root.padLeft
            y: (pill.height - height) / 2
            height: root.compactHeight
            implicitWidth: root.lead + 10 + liveText.implicitWidth

            Item {
                id: lead
                anchors.verticalCenter: parent.verticalCenter
                width: root.lead
                height: root.lead

                Waveform {
                    anchors.centerIn: parent
                    shown: root.lastMode === "listening"
                    level: root.island.level
                    color: liveFace.accent
                }
                Orb {
                    anchors.centerIn: parent
                    shown: root.lastMode === "thinking"
                    running: root.mode === "thinking"
                    color: liveFace.accent
                }
                DictationRing {
                    anchors.centerIn: parent
                    shown: root.lastMode === "dictating"
                    color: liveFace.accent
                }
                RecordDot {
                    anchors.centerIn: parent
                    shown: root.lastMode === "teaching"
                    color: liveFace.accent
                }
                Text {  // passthrough: another app's voice mode has the mic
                    anchors.centerIn: parent
                    text: "\uE720"
                    color: liveFace.accent
                    opacity: root.lastMode === "passthrough" ? 1 : 0
                    font.family: "Segoe Fluent Icons"
                    font.pixelSize: 16
                    Behavior on opacity { OpacityAnimator { duration: 160 } }
                }
            }

            LiveText {
                id: liveText
                anchors.left: lead.right
                anchors.leftMargin: 10
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: liveFace.noted ? root.island.note : root.island.words
                fresh: liveFace.noted ? 0 : root.island.fresh
                final: liveFace.noted || root.island.final
                windowed: !liveFace.noted && root.island.windowed
                placeholder: root.island.placeholder
            }
        }

        // acting: Chrome → Canvas → Today
        Face {
            id: actingFace
            active: root.mode === "acting"
            targetAccent: root.accent
            targetWidth: root.targetWidth - root.padLeft - root.padRight
            x: (pill.width - width - root.padLeft - root.padRight) / 2 + root.padLeft
            y: (pill.height - height) / 2
            height: root.compactHeight
            implicitWidth: root.lead + 10 + steps.implicitWidth

            Orb {
                id: actingOrb
                x: (root.lead - width) / 2
                anchors.verticalCenter: parent.verticalCenter
                running: root.mode === "acting"
                color: actingFace.accent
            }
            StepLine {
                id: steps
                anchors.left: parent.left
                anchors.leftMargin: root.lead + 10
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                steps: root.island.steps
                active: root.island.step
                running: root.mode === "acting"
            }
        }

        // done: ✓ / ⚡ + the result; collapses after 1.5 s (the state machine's timer)
        Face {
            id: doneFace
            active: root.mode === "done"
            targetAccent: root.accent
            targetWidth: root.targetWidth - root.padLeft - root.padRight
            x: (pill.width - width - root.padLeft - root.padRight) / 2 + root.padLeft
            y: (pill.height - height) / 2
            height: root.compactHeight
            implicitWidth: doneLine.x + doneLine.implicitWidth
            onEntered: doneLine.pop()

            ResultLine {
                id: doneLine
                x: (root.lead - 22) / 2
                width: parent.width - x
                anchors.verticalCenter: parent.verticalCenter
                icon: root.island.icon
                text: root.island.result
                accent: doneFace.accent
            }
        }

        // confirm: the exact action + [No] [Yes]
        Face {
            id: confirmFace
            active: root.mode === "confirm"
            targetAccent: root.accent
            targetWidth: root.targetWidth - 2 * root.expandedPadX
            x: (pill.width - width) / 2
            y: (pill.height - height) / 2
            height: implicitHeight
            implicitWidth: confirmRow.implicitWidth
            implicitHeight: confirmRow.implicitHeight

            ConfirmRow {
                id: confirmRow
                anchors.fill: parent
                question: root.island.question
                accent: confirmFace.accent
                onAnswered: (what) => root.island.act(what, -1, "")
            }
        }

        // clarify: a question + numbered choices
        Face {
            id: clarifyFace
            active: root.mode === "clarify"
            targetAccent: root.accent
            targetWidth: root.targetWidth - 2 * root.expandedPadX
            x: (pill.width - width) / 2
            y: (pill.height - height) / 2
            height: implicitHeight
            implicitWidth: choiceRow.implicitWidth
            implicitHeight: choiceRow.implicitHeight

            ChoiceRow {
                id: choiceRow
                anchors.fill: parent
                question: root.island.question
                choices: root.island.choices
                accent: clarifyFace.accent
                onPicked: (index) => root.island.act("choice", index, "")
            }
        }

        // error: what failed, plainly + [Try again] [Show me]
        Face {
            id: errorFace
            active: root.mode === "error"
            targetAccent: root.accent
            targetWidth: root.targetWidth - 2 * root.expandedPadX
            x: (pill.width - width) / 2
            y: (pill.height - height) / 2
            height: implicitHeight
            implicitWidth: Math.max(errorLine.implicitWidth, errorButtons.implicitWidth)
            implicitHeight: errorLine.implicitHeight + 14 + errorButtons.implicitHeight
            onEntered: errorLine.pop()

            ResultLine {
                id: errorLine
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(implicitWidth, parent.width)
                icon: "fail"
                text: root.island.result
                accent: errorFace.accent
                maximumLineCount: 2
            }
            Row {
                id: errorButtons
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.bottom: parent.bottom
                spacing: 10

                PillButton { text: "Try again"; onClicked: root.island.act("retry", -1, "") }
                PillButton { text: "Show me"; onClicked: root.island.act("show-me", -1, "") }
            }
        }
    }

    // --- chips: beside the pill, or alone at the top while it is closed ------------------------

    Chips {
        id: chips
        readonly property real targetX: root.open ? (root.width + root.targetWidth) / 2 + root.shift + 10 : (root.width - implicitWidth) / 2
        readonly property real targetY: root.open ? root.topMargin + (root.compactHeight - 30) / 2 : root.topMargin
        readonly property bool moving: chipsX.running || chipsY.running

        x: targetX
        y: targetY
        model: root.island.chips
        opacity: root.hasChips ? 1 : 0
        Behavior on x { SpringAnimation { id: chipsX; spring: 5; damping: 0.35; epsilon: 0.25 } }
        Behavior on y { SpringAnimation { id: chipsY; spring: 5; damping: 0.35; epsilon: 0.25 } }
        Behavior on opacity { OpacityAnimator { duration: 180 } }
        onClicked: (id) => root.island.act("chip", -1, id)
    }

    // --- tell Python where clicks may land, and when the window can hide -------------------------

    readonly property bool settled: !pill.morphing && !chips.moving && !shiftSpring.running && !dragArea.moving
    readonly property var hitRects: {
        const rects = []
        if (open)
            rects.push(Qt.rect((width - targetWidth) / 2 + shift, topMargin, targetWidth, targetHeight))
        if (hasChips)
            rects.push(Qt.rect(chips.targetX, chips.targetY, chips.implicitWidth, chips.implicitHeight))
        return rects
    }
    onHitRectsChanged: island.setHitArea(hitRects, settled)
    onSettledChanged: island.setHitArea(hitRects, settled)

    readonly property bool anything: open || hasChips
    onAnythingChanged: anything ? closing.stop() : closing.restart()

    Timer {  // one shot while the pill closes; then Python hides the window
        id: closing
        interval: 450
        onTriggered: if (!root.anything) root.island.collapsed()
    }
}
