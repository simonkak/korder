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
    readonly property int leadingW: 140
    // Wider default so most utterances render on a single line. Cap at the
    // screen width minus margins so the pill never spans the whole display.
    readonly property int minW: 720
    readonly property int maxW: Math.min(1200, Screen.width - 80)
    // Target preferred width for the center transcription text — used as
    // a hint to RowLayout so the window grows to a comfortable size before
    // wrapping kicks in.
    readonly property int centerTargetW: 540

    // ---- Colors ----
    // Bright (locked partial, committed text), faded-toward-bg (flux), and
    // the accent that the leading dot/icon picks up by state.
    readonly property color promptColor: palette.windowText
    readonly property color statusColor: palette.placeholderText
    // Flux: still-revising Whisper tail. Blend toward the window bg.
    readonly property color fluxColor: _blend(promptColor, palette.window, 0.45)
    // Hint: inline auxiliary info ("transcribing…", action name, pending
    // param prompt). Distinct from flux: italic + further toward bg, so
    // the eye can tell "this is system-state info" apart from "this is
    // text Whisper hasn't settled on yet".
    readonly property color hintColor: _blend(promptColor, palette.window, 0.35)

    // KDE Plasma accent for "active" states. Tied to palette.highlight when
    // available so themed users get their own accent; fallback to KDE blue.
    readonly property color accentColor:
        palette.highlight ? palette.highlight : Qt.rgba(0.24, 0.68, 0.91, 1.0)

    // Feedback (action narration) variant of the accent — blended toward the
    // theme's text color so it's legible as foreground text. palette.highlight
    // is tuned for selection-background contrast and reads dim on dark themes
    // when used as text. The blend keeps the accent's hue while lifting
    // luminance to text-brightness on dark themes (and pulling it down on
    // light themes), so it stays readable in both.
    readonly property color feedbackColor: _blend(palette.windowText, accentColor, 0.55)

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
    readonly property string hintHex: _toHex(hintColor)

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
        radius: 6
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

                // Leading: animated dot + state label.
                // fillHeight=true → leading section matches the row's height,
                // which grows with the center text when it wraps. The divider
                // inside is 8 px shorter than the section so it doesn't crowd
                // the rounded corners.
                Item {
                    id: leading
                    Layout.preferredWidth: root.leadingW
                    Layout.fillHeight: true
                    Layout.alignment: Qt.AlignVCenter

                    Rectangle {
                        id: divider1
                        width: 1
                        height: parent.height - 12
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

                // Center: prompt (locked) + flux (uncertain Whisper tail) +
                // hint (system-state aux info — "transcribing…", action name,
                // pending-param prompt) all inlined in one wrapping Text via
                // RichText. Three color tiers communicate "settled / still
                // deciding / system-context" without needing a separate chip.
                Item {
                    Layout.fillWidth: true
                    Layout.preferredWidth: root.centerTargetW
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredHeight: Math.max(40, promptText.implicitHeight + 16)

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: root.hPad
                        anchors.rightMargin: root.hPad
                        spacing: 6

                        // Blinking cursor (placeholder + pending states only).
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
                            // RichText whenever flux or status is present so
                            // we can color the segments differently. Plain
                            // text fallback for placeholder + feedback paths
                            // where the styling is uniform across the whole
                            // line (italic + alternate color).
                            readonly property bool _hasFlux:
                                osdState && osdState.flux !== undefined &&
                                osdState.flux.length > 0
                            readonly property bool _hasStatus:
                                osdState && osdState.status !== undefined &&
                                osdState.status.length > 0
                            readonly property bool _isFeedback:
                                osdState && osdState.feedbackMode === true
                            readonly property bool _useRich:
                                osdState && !osdState.placeholderMode &&
                                !_isFeedback && (_hasFlux || _hasStatus)

                            text: {
                                if (!osdState) return "";
                                if (osdState.placeholderMode || _isFeedback || !_useRich) {
                                    return osdState.prompt;
                                }
                                var parts = [];
                                parts.push('<font color="' + root.promptHex + '">' +
                                           root._escHtml(osdState.prompt) + '</font>');
                                if (_hasFlux) {
                                    parts.push('<font color="' + root.fluxHex + '">' +
                                               root._escHtml(osdState.flux) + '</font>');
                                }
                                if (_hasStatus) {
                                    // Subtle separator + italic hint. The
                                    // <i>...</i> turns italic on for just
                                    // this span; the <font color> dims it.
                                    var sep = osdState.prompt.length > 0 ? "  ·  " : "";
                                    parts.push('<font color="' + root.hintHex + '"><i>' +
                                               root._escHtml(sep + osdState.status) +
                                               '</i></font>');
                                }
                                return parts.join("");
                            }
                            textFormat: _useRich ? Text.RichText : Text.PlainText
                            // Color resolution priority:
                            //  - placeholder: muted statusColor
                            //  - feedback (action narration): Plasma's
                            //    accent — but the foreground-text variant
                            //    (feedbackColor), which lifts luminance so
                            //    palette.highlight doesn't read too dark on
                            //    Breeze Dark. Stays accent-hue, theme-adapts.
                            //  - normal: bright promptColor
                            color: {
                                if (!osdState) return root.promptColor;
                                if (osdState.placeholderMode) return root.statusColor;
                                if (_isFeedback) return root.feedbackColor;
                                return root.promptColor;
                            }
                            font.pixelSize: 16
                            font.weight: _isFeedback ? Font.Normal : Font.Medium
                            font.italic: osdState && (osdState.placeholderMode || _isFeedback)
                            wrapMode: Text.WordWrap
                            elide: Text.ElideRight
                            horizontalAlignment: Text.AlignLeft
                            verticalAlignment: Text.AlignVCenter
                            Layout.alignment: Qt.AlignVCenter
                            Layout.fillWidth: true
                            Layout.maximumWidth: root.maxW - root.leadingW - root.hPad * 3
                        }
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
