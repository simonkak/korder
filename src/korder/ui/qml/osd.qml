import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell

Window {
    id: root
    width: 520
    height: 56
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
        color: palette.window
        border.color: Qt.rgba(palette.windowText.r, palette.windowText.g, palette.windowText.b, 0.15)
        border.width: 1
        visible: osdState ? osdState.visible : false
        opacity: visible ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 120 } }

        Text {
            anchors.fill: parent
            anchors.margins: 14
            color: palette.windowText
            text: osdState ? osdState.text : ""
            font.pixelSize: 14
            font.weight: Font.Medium
            wrapMode: Text.WordWrap
            elide: Text.ElideRight
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }
}
