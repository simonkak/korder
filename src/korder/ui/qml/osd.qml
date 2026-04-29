import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell
import org.kde.ksvg as KSvg

Window {
    id: root
    width: 720
    height: 80
    color: "transparent"
    flags: Qt.FramelessWindowHint
    visible: true

    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.activateOnShow: false
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.margins.bottom: Math.round(Screen.height / 3)

    KSvg.FrameSvgItem {
        id: bg
        anchors.fill: parent
        // "widgets/tooltip" is the standard floating-translucent-with-blur
        // chrome KDE uses for tooltips and the volume/brightness OSD.
        // KWin's blur protocol reads the hint-blur-behind mask from this SVG
        // automatically — we don't have to ask for blur explicitly.
        imagePath: "widgets/tooltip"
        visible: osdState.visible
        opacity: visible ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 120 } }

        Text {
            anchors.fill: parent
            anchors.leftMargin: bg.margins.left + 12
            anchors.rightMargin: bg.margins.right + 12
            anchors.topMargin: bg.margins.top + 8
            anchors.bottomMargin: bg.margins.bottom + 8
            color: palette.windowText
            text: osdState.text
            font.pixelSize: 16
            font.weight: Font.Medium
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }

        SystemPalette { id: palette }
    }
}
