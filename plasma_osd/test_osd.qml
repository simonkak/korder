// Standalone QML test for the layer-shell OSD approach.
// Run with: qml6 plasma_osd/test_osd.qml
//
// What this verifies:
//   1. The window appears above all other windows (LayerOverlay)
//   2. It does NOT steal keyboard focus from your active app
//      (KeyboardInteractivityNone + activateOnShow: false)
//   3. The text updates without affecting focus or stacking
//
// To test: launch this, then click into another app and start typing.
// Your typing should land in that app, not be eaten by the OSD.
// Close with Ctrl+C in the terminal where you launched it.

import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell

Window {
    id: root
    width: 720
    height: 80
    visible: true
    color: "transparent"
    flags: Qt.FramelessWindowHint

    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.activateOnShow: false
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
    LayerShell.Window.exclusionZone: -1
    // Position-from-bottom margin: 1/3 of the screen above the bottom edge,
    // so the card sits visually at the 2/3-down mark.
    LayerShell.Window.margins.bottom: Math.round(Screen.height / 3)

    Rectangle {
        anchors.fill: parent
        radius: 14
        color: Qt.rgba(20/255, 22/255, 30/255, 0.9)

        Text {
            id: liveLabel
            anchors.fill: parent
            anchors.margins: 24
            color: "white"
            text: "Layer-shell OSD test — type in another window"
            font.pixelSize: 16
            font.weight: Font.Medium
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    Timer {
        interval: 2000
        running: true
        repeat: true
        property int tick: 0
        onTriggered: {
            tick += 1
            const phrases = [
                "Layer-shell OSD test — type in another window",
                "Tick " + tick + " — does this stay on top of everything?",
                "Click another window. Type. Did your text go there?",
                "If yes: success. If no: KWin's layer-shell needs work."
            ]
            liveLabel.text = phrases[tick % phrases.length]
        }
    }
}
