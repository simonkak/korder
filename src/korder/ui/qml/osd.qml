import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import org.kde.layershell as LayerShell

// Horizontal pill OSD, bottom-anchored. Three sections:
//   [accent dot + state label]│[transcription with locked/flux + cursor]│[status hint or lang chip]
// A small "press ESC to cancel" hint sits below the pill while the OSD is up.
//
// Background uses a semi-transparent fill so KWin's Blur compositor effect
// (enabled by default in Plasma 6) blurs whatever is behind us — true glass,
// no extra deps. With Blur disabled, we degrade to a translucent panel.

Window {
    id: root
    color: "transparent"
    flags: Qt.FramelessWindowHint
    visible: true

    readonly property int hPad: 14
    readonly property int vPad: 8
    readonly property int leadingW: 132
    readonly property int trailingMinW: 80
    readonly property int minW: 360
    readonly property int maxW: Math.min(900, Screen.width - 64)

    // ---- Colors ----
    // Bright (locked partial, committed text), faded-toward-bg (flux), and
    // the accent that the leading dot/icon picks up by state.
    readonly property color promptColor: palette.windowText
    readonly property color statusColor: palette.placeholderText
    readonly property color fluxColor: _blend(promptColor, palette.window, 0.45)

    // KDE Plasma blue for "active" states. Tied to palette.highlight when
    // available so themed users get their own accent; fallback to KDE blue.
    readonly property color accentColor:
        palette.highlight ? palette.highlight : Qt.rgba(0.24, 0.68, 0.91, 1.0)

    readonly property color accentForState: {
        if (!osdState) return accentColor;
        switch (osdState.stateKind) {
            case "listening": return accentColor;
            case "thinking":  return Qt.rgba(1.0, 0.73, 0.33, 1.0); // amber
            case "executing": return Qt.rgba(0.45, 0.85, 0.45, 1.0); // green
            case "pending":   return Qt.rgba(1.0, 0.73, 0.33, 1.0); // amber
            case "committed": return Qt.rgba(0.45, 0.85, 0.45, 1.0); // green
        }
        return accentColor;
    }

    readonly property string promptHex: _toHex(promptColor)
    readonly property string fluxHex: _toHex(fluxColor)

    function _toHex(c) {
        function pad(n) { return ('0' + Math.round(n * 255).toString(16)).slice(-2); }
        return '#' + pad(c.r) + pad(c.g) + pad(c.b);
    }

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
    LayerShell.Window.margins.bottom: 56

    SystemPalette { id: palette; colorGroup: SystemPalette.Active }

    Rectangle {
        id: bg
        anchors.fill: parent
        radius: 10
        // Semi-transparent fill — KWin blurs it automatically via the Blur
        // compositor effect (default-on in Plasma 6).
        color: Qt.rgba(palette.window.r, palette.window.g, palette.window.b, 0.86)
        border.color: Qt.rgba(palette.windowText.r, palette.windowText.g, palette.windowText.b, 0.18)
        border.width: 1
        visible: osdState ? osdState.visible : false
        opacity: visible ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 140 } }

        ColumnLayout {
            id: contentColumn
            anchors.fill: parent
            anchors.margins: 0
            spacing: 4

            // ---------- The pill itself: 3 sections in a Row ----------
            RowLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                spacing: 0

                // Leading: animated dot + state label
                Item {
                    id: leading
                    Layout.preferredWidth: root.leadingW
                    Layout.preferredHeight: 36
                    Layout.alignment: Qt.AlignVCenter

                    Rectangle {
                        id: divider1
                        width: 1
                        height: parent.height - 8
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        color: Qt.rgba(palette.windowText.r, palette.windowText.g, palette.windowText.b, 0.12)
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: root.hPad
                        anchors.rightMargin: 8
                        spacing: 8

                        // Pulsing accent dot (replaces the old blink-cursor)
                        Item {
                            id: dotHolder
                            Layout.preferredWidth: 14
                            Layout.preferredHeight: 14
                            Layout.alignment: Qt.AlignVCenter

                            // Outer ring — pulses (scale + fade) for "listening"
                            // and "pending"; static for thinking/executing/done.
                            Rectangle {
                                id: pulseRing
                                anchors.centerIn: parent
                                width: 14; height: 14
                                radius: width / 2
                                color: "transparent"
                                border.color: root.accentForState
                                border.width: 2
                                opacity: 0.0
                                SequentialAnimation on opacity {
                                    running: osdState && (osdState.stateKind === "listening" || osdState.stateKind === "pending")
                                    loops: Animation.Infinite
                                    NumberAnimation { from: 0.55; to: 0.0; duration: 1100; easing.type: Easing.OutCubic }
                                    NumberAnimation { duration: 200 }
                                }
                                SequentialAnimation on scale {
                                    running: osdState && (osdState.stateKind === "listening" || osdState.stateKind === "pending")
                                    loops: Animation.Infinite
                                    NumberAnimation { from: 1.0; to: 1.85; duration: 1100; easing.type: Easing.OutCubic }
                                    NumberAnimation { duration: 200 }
                                }
                            }

                            // Inner solid dot — also gently pulses for "thinking"
                            // (fade only, no scale) so user knows we're alive.
                            Rectangle {
                                id: dot
                                anchors.centerIn: parent
                                width: 8; height: 8
                                radius: width / 2
                                color: root.accentForState
                                SequentialAnimation on opacity {
                                    running: osdState && osdState.stateKind === "thinking"
                                    loops: Animation.Infinite
                                    NumberAnimation { from: 1.0; to: 0.35; duration: 600 }
                                    NumberAnimation { from: 0.35; to: 1.0; duration: 600 }
                                }
                            }
                        }

                        Text {
                            id: stateText
                            text: osdState ? osdState.stateLabel : ""
                            color: palette.windowText
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                            font.letterSpacing: 0.2
                            elide: Text.ElideRight
                            Layout.alignment: Qt.AlignVCenter
                            Layout.fillWidth: true
                        }
                    }
                }

                // Center: transcription with locked/flux highlighting + optional cursor
                Item {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredHeight: Math.max(36, promptText.implicitHeight + 12)

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: root.hPad
                        anchors.rightMargin: root.hPad
                        spacing: 6

                        // Blinking cursor (only shown for placeholder + pending states)
                        Rectangle {
                            id: cursor
                            Layout.alignment: Qt.AlignVCenter
                            width: 2
                            height: promptText.font.pixelSize
                            color: root.accentForState
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
                            // RichText only when we have a flux tail; otherwise
                            // PlainText so italics/colors apply normally.
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
                            font.pixelSize: 16
                            font.weight: Font.Medium
                            font.italic: osdState ? osdState.placeholderMode : false
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight
                            horizontalAlignment: Text.AlignLeft
                            verticalAlignment: Text.AlignVCenter
                            Layout.alignment: Qt.AlignVCenter
                            Layout.fillWidth: true
                            Layout.maximumWidth: root.maxW - root.leadingW - root.trailingMinW - root.hPad * 4
                        }
                    }
                }

                // Trailing: status hint (Thinking / Executing description / pending param hint)
                Item {
                    visible: osdState && osdState.status.length > 0
                    Layout.preferredWidth: visible ? Math.min(220, statusChipText.implicitWidth + 28) : 0
                    Layout.preferredHeight: 36
                    Layout.alignment: Qt.AlignVCenter

                    Rectangle {
                        id: divider2
                        width: 1
                        height: parent.height - 8
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        color: Qt.rgba(palette.windowText.r, palette.windowText.g, palette.windowText.b, 0.12)
                        visible: parent.visible
                    }

                    Text {
                        id: statusChipText
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: root.hPad
                        text: osdState ? osdState.status : ""
                        color: root.statusColor
                        font.pixelSize: 12
                        font.italic: true
                        verticalAlignment: Text.AlignVCenter
                        horizontalAlignment: Text.AlignRight
                        elide: Text.ElideRight
                    }
                }
            }
        }
    }

    // ---------- Hint below the pill: "Press ESC to cancel" ----------
    // Only shown during listening — disappears once we transition to
    // thinking/executing where cancellation isn't meaningful anymore.
    Text {
        id: cancelHint
        anchors.top: bg.bottom
        anchors.topMargin: 6
        anchors.horizontalCenter: bg.horizontalCenter
        text: (osdState && osdState.stateKind === "listening" && osdState.escHint)
                ? osdState.escHint
                : ""
        color: Qt.rgba(palette.windowText.r, palette.windowText.g, palette.windowText.b, 0.55)
        font.pixelSize: 10
        font.letterSpacing: 0.2
        visible: bg.visible && text.length > 0
    }
}
