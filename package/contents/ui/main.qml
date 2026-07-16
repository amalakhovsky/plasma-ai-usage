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

    readonly property string helperPath:
        Qt.resolvedUrl("../tools/aiusage.py").toString().replace("file://", "")
    readonly property string helperCmd: "python3 '" + helperPath + "'"

    readonly property int pollSeconds: Plasmoid.configuration.pollSeconds || 120
    readonly property string panelMode: Plasmoid.configuration.panelWindow || "shortest"
    readonly property bool showPercent: Plasmoid.configuration.showPercent
    readonly property int ringSize: Plasmoid.configuration.ringSize || 18

    // A provider block is shown when the helper reports it available.
    function provAvailable(name) {
        return usage && usage[name] && usage[name].available;
    }
    function provWindow(name) {
        if (!provAvailable(name)) return null;
        return Theme.panelWindow(usage[name].windows, panelMode);
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

    // Collapse refreshes closer than 60s (popup-open + timer can otherwise
    // fire back-to-back and get rate-limited).
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

    // Refresh when the popup opens.
    onExpandedChanged: if (expanded) refresh(false)

    // Tick reset countdowns in the popup once a minute without refetching.
    Timer {
        interval: 60000; running: root.expanded; repeat: true
        onTriggered: root.lastUpdated = root.lastUpdated // no-op nudge for bindings
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

    // ---- one panel ring + optional % for a provider ----
    component ProviderChip: RowLayout {
        id: chip
        property string provider: "claude"
        property var win: root.provWindow(provider)
        property var prov: root.usage ? root.usage[provider] : null
        spacing: 3
        visible: root.provAvailable(provider)

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
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            Layout.alignment: Qt.AlignVCenter
        }
    }

    compactRepresentation: MouseArea {
        Layout.minimumWidth: rings.implicitWidth + 4
        implicitWidth: rings.implicitWidth + 4
        onClicked: root.expanded = !root.expanded
        RowLayout {
            id: rings
            anchors.centerIn: parent
            spacing: 8
            ProviderChip { provider: "claude" }
            ProviderChip { provider: "codex" }
            PlasmaComponents.Label {
                visible: !root.provAvailable("claude") && !root.provAvailable("codex")
                text: root.usage ? "AI —" : "…"
                opacity: 0.6
            }
        }
    }

    // ---- popup ----
    fullRepresentation: ColumnLayout {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 15
        Layout.preferredWidth: Kirigami.Units.gridUnit * 16
        Layout.preferredHeight: implicitHeight
        spacing: Kirigami.Units.smallSpacing

        // Provider section builder
        component ProviderSection: ColumnLayout {
            property string provider: "claude"
            property string title: "Claude"
            property var prov: root.usage ? root.usage[provider] : null
            visible: root.provAvailable(provider)
            spacing: 4
            Layout.fillWidth: true

            RowLayout {
                Layout.fillWidth: true
                Rectangle {
                    width: 10; height: 10; radius: 5
                    color: Theme.brand(provider)
                    Layout.alignment: Qt.AlignVCenter
                }
                PlasmaComponents.Label { text: title; font.bold: true }
                Item { Layout.fillWidth: true }
                PlasmaComponents.Label {
                    text: prov ? (prov.tier || prov.plan || "") : ""
                    opacity: 0.7
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                }
            }

            PlasmaComponents.Label {
                visible: prov && prov.error
                text: prov ? (prov.error || "") : ""
                color: Kirigami.Theme.negativeTextColor
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
            }

            Repeater {
                model: prov && prov.windows ? prov.windows : []
                delegate: Meter {
                    Layout.fillWidth: true
                    provider: prov ? (provider) : "claude"
                    label: modelData.label
                    percent: modelData.percent
                    severity: modelData.severity
                    resetsAt: modelData.resetsAt || ""
                    dim: !modelData.active
                }
            }

            // Claude credit / spend
            Meter {
                visible: prov !== null && prov !== undefined && prov.spend !== null && prov.spend !== undefined
                Layout.fillWidth: true
                provider: "claude"
                label: prov && prov.spend
                    ? "Credits  " + (prov.spend.currency || "") + " " + prov.spend.used + " / " + prov.spend.limit
                    : "Credits"
                percent: prov && prov.spend ? prov.spend.percent : 0
                severity: prov && prov.spend ? prov.spend.severity : "ok"
            }
        }

        ProviderSection { provider: "claude"; title: "Claude" }

        Rectangle {
            visible: root.provAvailable("claude") && root.provAvailable("codex")
            Layout.fillWidth: true; height: 1
            color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                           Kirigami.Theme.textColor.b, 0.15)
        }

        ProviderSection { provider: "codex"; title: "Codex" }

        PlasmaComponents.Label {
            visible: !root.provAvailable("claude") && !root.provAvailable("codex")
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: root.loadError.length ? root.loadError
                : "No Claude or Codex credentials found.\nSign in with the claude or codex CLI."
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
