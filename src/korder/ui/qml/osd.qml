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

    Rectangle {
        anchors.fill: parent
        radius: 14
        color: Qt.rgba(20/255, 22/255, 30/255, 0.9)
        visible: osdState.visible
        opacity: visible ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 120 } }

        Text {
            anchors.fill: parent
            anchors.margins: 24
            color: "white"
            text: osdState.text
            font.pixelSize: 16
            font.weight: Font.Medium
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }
}
