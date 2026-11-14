import QtQuick

// The talk-session pill (SPEC §5, §6): one pill that stays up between commands while a talk
// session is open. Row 1: the mic level, his words rolling in, and Stop (closes the session and
// cancels what runs, like Esc). Row 2, only while there is something to say: the orb while Nova
// thinks, the step line while it acts ("Notepad → new note"), ✓ + the result when a clause is
// done, the red result + what was skipped when one failed. The pill grows to fit row 2.
Face {
    id: face

    required property var island
    property real lead: 24  // the indicator column (waveform above, orb or badge below)
    readonly property real rowHeight: 44  // row 1 is the compact pill's one line
    readonly property real belowHeight: 28
    readonly property bool below: island.beneath !== "" || island.skipped !== ""
    property string shown: ""  // what row 2 shows; kept while it fades out (the pill shrinks around it)

    function latch() {
        if (face.island.beneath !== "" || face.island.skipped !== "")
            face.shown = face.island.beneath
        if (face.island.beneath === "done" || face.island.beneath === "error")
            outcomeLine.pop()
    }

    implicitWidth: 420
    implicitHeight: below ? rowHeight + belowHeight : rowHeight

    Connections {
        target: face.island
        function onBeneathChanged() { face.latch() }
        function onSkippedChanged() { face.latch() }
    }

    // --- row 1: mic level · his words · Stop ----------------------------------------------------

    Item {
        id: top
        width: parent.width
        height: face.rowHeight

        Waveform {
            x: (face.lead - width) / 2
            anchors.verticalCenter: parent.verticalCenter
            level: face.island.level
            color: face.accent
        }
        LiveText {
            anchors.left: parent.left
            anchors.leftMargin: face.lead + 10
            anchors.right: stop.left
            anchors.rightMargin: 12
            anchors.verticalCenter: parent.verticalCenter
            text: face.island.words
            fresh: face.island.fresh
            final: face.island.final
            windowed: face.island.windowed
            placeholder: face.island.placeholder
        }
        PillButton {
            id: stop
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            implicitWidth: 66
            implicitHeight: 28
            text: "Stop"
            onClicked: face.island.act("stop-session", -1, "")
        }
    }

    // --- row 2: thinking · acting · done · failed + skipped -------------------------------------

    Item {
        id: under
        y: face.rowHeight - 3
        width: parent.width
        height: face.belowHeight
        opacity: face.below ? 1 : 0
        visible: face.below || opacity > 0  // an animator writes opacity only when it ends
        Behavior on opacity { OpacityAnimator { duration: face.below ? 200 : 110 } }

        Orb {
            id: orb
            x: (face.lead - width) / 2
            anchors.verticalCenter: parent.verticalCenter
            shown: face.shown === "thinking" || face.shown === "acting"
            running: face.active && face.below && shown
            color: face.accent
        }
        Text {  // the local model is cold and loads first
            anchors.left: parent.left
            anchors.leftMargin: face.lead + 10
            anchors.verticalCenter: parent.verticalCenter
            visible: face.shown === "thinking" && face.island.warming
            text: "Warming up…"
            color: "#FFFFFF"
            opacity: 0.6
            textFormat: Text.PlainText
            font.family: "Segoe UI Variable Text"
            font.pixelSize: 14
        }
        StepLine {
            anchors.left: parent.left
            anchors.leftMargin: face.lead + 10
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            visible: face.shown === "acting"
            steps: face.island.steps
            active: face.island.step
            running: face.active && face.island.beneath === "acting"
        }
        Item {
            id: outcome
            readonly property bool failed: face.shown === "error"
            readonly property bool result: face.shown === "done" || failed

            anchors.left: parent.left
            anchors.leftMargin: (face.lead - 22) / 2
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            height: 22
            visible: result || face.shown === ""

            ResultLine {
                id: outcomeLine
                anchors.verticalCenter: parent.verticalCenter
                visible: outcome.result
                width: Math.min(implicitWidth, skippedLine.text ? outcome.width * 0.55 : outcome.width)
                icon: outcome.failed ? "fail" : face.island.icon
                text: face.island.result
                accent: outcome.failed ? "#FF453A" : "#30D158"
            }
            Text {  // "Skipped: create a new note, make the title…" (cut in view.py, elided here if need be)
                id: skippedLine
                anchors.left: outcome.result ? outcomeLine.right : parent.left
                anchors.leftMargin: outcome.result ? 10 : face.lead + 10 - outcome.anchors.leftMargin
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: face.island.skipped
                color: "#FFFFFF"
                opacity: 0.6
                elide: Text.ElideRight
                textFormat: Text.PlainText
                font.family: "Segoe UI Variable Text"
                font.weight: Font.Medium
                font.pixelSize: 13
            }
        }
    }
}
