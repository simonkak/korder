import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell

Window {
    id: root
    // Adaptive size: shrinks for short text, caps at MAX_W above which content wraps.
    readonly property int hPad: 24
    readonly property int vPad: 14
    readonly property int minW: 220
    // Cap below screen width so we don't slam against the edges; long
    // sentences grow horizontally rather than wrap to multiple lines.
    readonly property int maxW: Math.max(minW, Screen.width - 120)
    width: Math.max(minW, Math.min(maxW, textItem.width + hPad * 2))
    height: Math.max(44, textItem.implicitHeight + vPad * 2)
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
            id: textItem
            anchors.centerIn: parent
            // If natural width fits within max, use it; otherwise cap and wrap.
            width: Math.min(implicitWidth, root.maxW - root.hPad * 2)
            color: palette.windowText
            text: osdState ? osdState.text : ""
            font.pixelSize: 13
            font.weight: Font.Medium
            wrapMode: Text.WordWrap
            elide: Text.ElideRight
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }
}
