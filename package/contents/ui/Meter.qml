import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import "Theme.js" as Theme

// A labelled progress meter for the popup: name + % row, a bar, and a reset
// caption. Color follows brand-hue + severity, matching the panel ring.
ColumnLayout {
    id: root

    property string label: ""
    property real percent: 0
    property string severity: "ok"
    property string provider: "claude"
    property string resetsAt: ""
    property bool dim: false            // inactive windows render muted

    spacing: 2
    Layout.fillWidth: true

    RowLayout {
        Layout.fillWidth: true
        PlasmaComponents.Label {
            text: root.label
            Layout.fillWidth: true
            elide: Text.ElideRight
            opacity: root.dim ? 0.6 : 1.0
            font.pointSize: Kirigami.Theme.smallFont.pointSize
        }
        PlasmaComponents.Label {
            text: Math.round(root.percent) + "%"
            opacity: root.dim ? 0.6 : 1.0
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            font.bold: true
            color: Theme.rampColor(root.provider, root.severity)
        }
    }

    Rectangle {
        Layout.fillWidth: true
        height: 5
        radius: height / 2
        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                       Kirigami.Theme.textColor.b, 0.18)
        Rectangle {
            height: parent.height
            radius: parent.radius
            width: parent.width * Math.max(0, Math.min(100, root.percent)) / 100
            color: Theme.rampColor(root.provider, root.severity)
            opacity: root.dim ? 0.7 : 1.0
            Behavior on width { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }
        }
    }

    PlasmaComponents.Label {
        text: Theme.relativeReset(root.resetsAt)
        visible: text.length > 0
        opacity: 0.65
        font.pointSize: Kirigami.Theme.smallFont.pointSize
    }
}
