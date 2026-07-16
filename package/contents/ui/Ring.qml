import QtQuick
import org.kde.kirigami as Kirigami
import "Theme.js" as Theme

// A compact circular usage gauge, drawn with Canvas. Track arc in the theme's
// text color (faint) + a value arc from 12 o'clock, colored brand-hue+severity.
Item {
    id: root

    property real percent: 0            // 0..100
    property string severity: "ok"      // ok | warn | crit
    property string provider: "claude"  // brand hue key
    property int ringSize: 18
    property int lineWidth: 3

    implicitWidth: ringSize
    implicitHeight: ringSize

    onPercentChanged: canvas.requestPaint()
    onSeverityChanged: canvas.requestPaint()
    onProviderChanged: canvas.requestPaint()

    Canvas {
        id: canvas
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d");
            ctx.reset();
            var w = width, h = height;
            var cx = w / 2, cy = h / 2;
            var r = Math.min(w, h) / 2 - root.lineWidth / 2;
            var start = -Math.PI / 2;

            ctx.lineWidth = root.lineWidth;
            ctx.lineCap = "round";

            // track
            var tc = Kirigami.Theme.textColor;
            ctx.strokeStyle = Qt.rgba(tc.r, tc.g, tc.b, 0.22);
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, 2 * Math.PI);
            ctx.stroke();

            // value
            var p = Math.max(0, Math.min(100, root.percent));
            if (p > 0) {
                ctx.strokeStyle = Theme.rampColor(root.provider, root.severity);
                ctx.beginPath();
                ctx.arc(cx, cy, r, start, start + (p / 100) * 2 * Math.PI);
                ctx.stroke();
            }
        }
    }

    // Re-tint on theme (light/dark) changes.
    Connections {
        target: Kirigami.Theme
        function onTextColorChanged() { canvas.requestPaint(); }
    }
}
