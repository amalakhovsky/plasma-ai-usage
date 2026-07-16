import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.kirigami as Kirigami
import "Theme.js" as Theme

PlasmoidItem {
    id: root

    property var usage: null          // parsed helper output
    property double lastUpdated: 0
    property string loadError: ""
    property double lastFetch: 0
    // Which provider's quotas the popup shows — set by the ring you click.
    property string selectedProvider: "claude"

    readonly property string helperPath:
        Qt.resolvedUrl("../tools/aiusage.py").toString().replace("file://", "")
    readonly property string helperCmd: "python3 '" + helperPath + "'"

    readonly property int pollSeconds: Plasmoid.configuration.pollSeconds || 120
    readonly property string panelMode: Plasmoid.configuration.panelWindow || "shortest"
    readonly property bool showPercent: Plasmoid.configuration.showPercent
    readonly property int ringSize: Plasmoid.configuration.ringSize || 22

    function provAvailable(name) {
        return usage && usage[name] && usage[name].available;
    }
    function provWindow(name) {
        if (!provAvailable(name)) return null;
        return Theme.panelWindow(usage[name].windows, panelMode);
    }
    function titleFor(name) {
        return name.charAt(0).toUpperCase() + name.slice(1);
    }
    // Click a ring: open that provider's popup, or close it if it's already the
    // one showing.
    function selectProvider(name) {
        if (root.expanded && root.selectedProvider === name) {
            root.expanded = false;
        } else {
            root.selectedProvider = name;
            root.expanded = true;
        }
    }

    Plasma5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: function (source, data) {
            exec.disconnectSource(source);
            if ((data["exit code"] || 0) === 0 && data["stdout"]) {
                try {
                    root.usage = JSON.parse(data["stdout"]);
                    root.lastUpdated = Date.now();
                    root.loadError = "";
                } catch (e) {
                    root.loadError = "bad helper output";
                }
            } else {
                root.loadError = (data["stderr"] || "helper failed").trim();
            }
        }
        function poll() { exec.connectSource(root.helperCmd); }
    }

    function refresh(force) {
        if (!force && Date.now() - lastFetch < 60000) return;
        lastFetch = Date.now();
        exec.poll();
    }

    Timer {
        interval: root.pollSeconds * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh(false)
    }

    onExpandedChanged: if (expanded) refresh(false)

    // Tick reset countdowns in the popup once a minute without refetching.
    Timer {
        interval: 60000; running: root.expanded; repeat: true
        onTriggered: root.lastUpdated = root.lastUpdated
    }

    Plasmoid.status: (provAvailable("claude") || provAvailable("codex"))
        ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus

    toolTipMainText: "AI usage"
    toolTipSubText: {
        if (!usage) return "Loading…";
        var parts = [];
        var c = provWindow("claude"); if (c) parts.push("Claude " + Math.round(c.percent) + "%");
        var x = provWindow("codex");  if (x) parts.push("Codex " + Math.round(x.percent) + "%");
        return parts.length ? parts.join("   ·   ") : "No providers detected";
    }

    // ---- one clickable panel ring + optional % for a provider ----
    component ProviderChip: MouseArea {
        id: chip
        property string provider: "claude"
        property var win: root.provWindow(provider)
        property var prov: root.usage ? root.usage[provider] : null

        visible: root.provAvailable(provider)
        implicitWidth: chipRow.implicitWidth
        implicitHeight: Math.max(root.ringSize, chipRow.implicitHeight)
        Layout.fillHeight: true
        cursorShape: Qt.PointingHandCursor
        hoverEnabled: true
        onClicked: root.selectProvider(provider)

        RowLayout {
            id: chipRow
            anchors.centerIn: parent
            spacing: 4
            opacity: chip.containsMouse ? 0.8 : 1.0

            Ring {
                provider: chip.provider
                ringSize: root.ringSize
                Layout.preferredWidth: root.ringSize
                Layout.preferredHeight: root.ringSize
                Layout.alignment: Qt.AlignVCenter
                percent: chip.win ? chip.win.percent : 0
                severity: (chip.prov && chip.prov.error) ? "crit"
                          : (chip.win ? chip.win.severity : "ok")
            }
            PlasmaComponents.Label {
                visible: root.showPercent && chip.win
                text: (chip.prov && chip.prov.error) ? "!" : (chip.win ? Math.round(chip.win.percent) + "%" : "")
                font.bold: true
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    compactRepresentation: RowLayout {
        id: rings
        spacing: 10
        Layout.minimumWidth: implicitWidth
        ProviderChip { provider: "claude" }
        ProviderChip { provider: "codex" }
        MouseArea {
            visible: !root.provAvailable("claude") && !root.provAvailable("codex")
            implicitWidth: fallback.implicitWidth
            implicitHeight: fallback.implicitHeight
            onClicked: root.expanded = !root.expanded
            PlasmaComponents.Label { id: fallback; text: root.usage ? "AI —" : "…"; opacity: 0.6 }
        }
    }

    // ---- popup: shows ONLY the provider whose ring you clicked ----
    fullRepresentation: ColumnLayout {
        id: popup
        Layout.minimumWidth: Kirigami.Units.gridUnit * 15
        Layout.preferredWidth: Kirigami.Units.gridUnit * 16
        Layout.preferredHeight: implicitHeight
        spacing: Kirigami.Units.smallSpacing

        readonly property string sp: root.selectedProvider
        readonly property var prov: root.usage ? root.usage[sp] : null

        // header
        RowLayout {
            Layout.fillWidth: true
            Rectangle {
                width: 12; height: 12; radius: 6
                color: Theme.brand(popup.sp)
                Layout.alignment: Qt.AlignVCenter
            }
            PlasmaComponents.Label { text: root.titleFor(popup.sp); font.bold: true }
            Item { Layout.fillWidth: true }
            PlasmaComponents.Label {
                text: popup.prov ? (popup.prov.tier || popup.prov.plan || "") : ""
                opacity: 0.7
                font.pointSize: Kirigami.Theme.smallFont.pointSize
            }
        }

        PlasmaComponents.Label {
            visible: popup.prov && popup.prov.error
            text: popup.prov ? (popup.prov.error || "") : ""
            color: Kirigami.Theme.negativeTextColor
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
        }

        Repeater {
            model: popup.prov && popup.prov.windows ? popup.prov.windows : []
            delegate: Meter {
                Layout.fillWidth: true
                provider: root.selectedProvider
                label: modelData.label
                percent: modelData.percent
                severity: modelData.severity
                resetsAt: modelData.resetsAt || ""
                dim: !modelData.active
            }
        }

        // Claude credit / spend (only when the provider reports spend)
        Meter {
            visible: popup.prov && popup.prov.spend !== null && popup.prov.spend !== undefined
            Layout.fillWidth: true
            provider: root.selectedProvider
            label: (popup.prov && popup.prov.spend)
                ? "Credits  " + (popup.prov.spend.currency || "") + " " + popup.prov.spend.used + " / " + popup.prov.spend.limit
                : "Credits"
            percent: (popup.prov && popup.prov.spend) ? popup.prov.spend.percent : 0
            severity: (popup.prov && popup.prov.spend) ? popup.prov.spend.severity : "ok"
        }

        PlasmaComponents.Label {
            visible: !popup.prov || !popup.prov.available
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: root.loadError.length ? root.loadError
                : "No credentials found.\nSign in with the claude or codex CLI."
            opacity: 0.8
        }

        Item { Layout.fillWidth: true; height: Kirigami.Units.smallSpacing }

        RowLayout {
            Layout.fillWidth: true
            PlasmaComponents.Label {
                Layout.fillWidth: true
                opacity: 0.6
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                text: root.lastUpdated
                    ? "Updated " + Qt.formatTime(new Date(root.lastUpdated))
                    : "Loading…"
            }
            PlasmaComponents.Button {
                text: "Refresh"
                icon.name: "view-refresh-symbolic"
                onClicked: root.refresh(true)
            }
        }
    }
}
