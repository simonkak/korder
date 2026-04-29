import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell

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

    SystemPalette { id: palette; colorGroup: SystemPalette.Active }

    Rectangle {
        id: bg
        anchors.fill: parent
        radius: 10
        // Follow system palette; small alpha so KWin's blur (when granted)
        // shows through. Without blur it still reads as a solid translucent
        // panel matching the active theme's window color.
        color: Qt.rgba(palette.window.r, palette.window.g, palette.window.b, 0.82)
        border.color: Qt.rgba(palette.windowText.r, palette.windowText.g, palette.windowText.b, 0.10)
        border.width: 1
        visible: osdState.visible
        opacity: visible ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 120 } }

        Text {
            anchors.fill: parent
            anchors.margins: 18
            color: palette.windowText
            text: osdState.text
            font.pixelSize: 16
            font.weight: Font.Medium
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }
}
