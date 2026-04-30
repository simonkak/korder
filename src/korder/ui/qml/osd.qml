import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import org.kde.layershell as LayerShell

Window {
    id: root
    color: "transparent"
    flags: Qt.FramelessWindowHint
    visible: true

    readonly property int hPad: 24
    readonly property int vPad: 14
    readonly property int minW: 320
    readonly property int maxW: Math.max(minW, Screen.width - 120)

    // Two text colors:
    //  - prompt: bright (palette.windowText)  — user's words
    //  - status: faded (palette.placeholderText) — hints, thinking, etc.
    readonly property color promptColor: palette.windowText
    readonly property color statusColor: palette.placeholderText

    width: Math.max(minW, Math.min(maxW, contentColumn.implicitWidth + hPad * 2))
    height: contentColumn.implicitHeight + vPad * 2

    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.activateOnShow: false
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.margins.bottom: Math.round(Screen.height / 3)

    SystemPalette { id: palette; colorGroup: SystemPalette.Active }

    // Background card (always renders when osdState.visible — stays inside
    // the always-mapped layer-shell surface so we don't re-map per state).
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

        ColumnLayout {
            id: contentColumn
            anchors.centerIn: parent
            spacing: 4
            width: Math.min(implicitWidth, root.maxW - root.hPad * 2)

            // Top row: blinking cursor (if active) + prompt text
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 6

                Rectangle {
                    id: cursor
                    Layout.alignment: Qt.AlignVCenter
                    width: 2
                    height: promptText.font.pixelSize
                    color: root.promptColor
                    visible: osdState ? osdState.showCursor : false
                    SequentialAnimation on opacity {
                        running: cursor.visible
                        loops: Animation.Infinite
                        NumberAnimation { from: 1.0; to: 0.0; duration: 500 }
                        NumberAnimation { from: 0.0; to: 1.0; duration: 500 }
                    }
                }

                Text {
                    id: promptText
                    text: osdState ? osdState.prompt : ""
                    color: osdState && osdState.placeholderMode
                        ? root.statusColor
                        : root.promptColor
                    font.pixelSize: 18
                    font.weight: Font.Medium
                    font.italic: osdState ? osdState.placeholderMode : false
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    elide: Text.ElideRight
                    Layout.maximumWidth: root.maxW - root.hPad * 2 - 16
                }
            }

            // Bottom row: faded italic status hint, only when status is non-empty
            Text {
                id: statusText
                visible: osdState && osdState.status.length > 0
                text: osdState ? osdState.status : ""
                color: root.statusColor
                font.pixelSize: 12
                font.italic: true
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
                Layout.maximumWidth: root.maxW - root.hPad * 2 - 16
                wrapMode: Text.WordWrap
                elide: Text.ElideRight
            }
        }
    }
}
