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

    // Three text colors:
    //  - prompt: bright (palette.windowText)  — locked partial + final text
    //  - status: faded (palette.placeholderText) — hints, thinking
    //  - flux:   blended toward window bg     — in-flux partial tail; needs to
    //            be obviously dimmer than prompt regardless of theme, so we
    //            blend the text color toward the background rather than relying
    //            on the theme's placeholderText (which can be subtle).
    readonly property color promptColor: palette.windowText
    readonly property color statusColor: palette.placeholderText
    readonly property color fluxColor: _blend(promptColor, palette.window, 0.45)

    // Cached hex strings for the colors (Text.RichText needs CSS-style hex
    // codes inside <font color>; recomputed when the palette changes).
    readonly property string promptHex: _toHex(promptColor)
    readonly property string fluxHex: _toHex(fluxColor)

    function _toHex(c) {
        function pad(n) { return ('0' + Math.round(n * 255).toString(16)).slice(-2); }
        return '#' + pad(c.r) + pad(c.g) + pad(c.b);
    }

    // Linear blend: alpha=0 returns bg, alpha=1 returns fg.
    function _blend(fg, bg, alpha) {
        return Qt.rgba(
            fg.r * alpha + bg.r * (1 - alpha),
            fg.g * alpha + bg.g * (1 - alpha),
            fg.b * alpha + bg.b * (1 - alpha),
            1.0
        );
    }

    function _escHtml(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

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
                    // Two render modes:
                    //  - PlainText: placeholder, committed text, or partial
                    //    with no flux (single-color, italic if placeholder).
                    //  - RichText: streaming partial with a locked-prefix +
                    //    flux-tail split — render the locked region in
                    //    promptColor and the flux tail in statusColor so
                    //    the eye can ignore the still-revising tail.
                    readonly property bool _useRich:
                        osdState && !osdState.placeholderMode &&
                        osdState.flux !== undefined && osdState.flux.length > 0

                    text: _useRich
                        ? '<font color="' + root.promptHex + '">' + root._escHtml(osdState.prompt) + '</font>' +
                          '<font color="' + root.fluxHex + '">' + root._escHtml(osdState.flux) + '</font>'
                        : (osdState ? osdState.prompt : "")
                    textFormat: _useRich ? Text.RichText : Text.PlainText
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

            // Bottom row: faded italic status hint. Always renders (non-breaking
            // space when empty) so the OSD's overall height stays stable across
            // state transitions instead of jumping when status appears/clears.
            Text {
                id: statusText
                text: (osdState && osdState.status.length > 0) ? osdState.status : " "
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
