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
        + " --or-floors " + Number(Plasmoid.configuration.orWarnUsd) + "," + Number(Plasmoid.configuration.orCritUsd)
        + (Plasmoid.configuration.orNotify ? "" : " --no-notify")
    readonly property var providerNames: ["claude", "codex", "agy", "openrouter"]
    readonly property string orLabel: Plasmoid.configuration.orLabel || "dollars"

    // 5 min default: the usage endpoints 429 easily and the helper caches +
    // backs off on its own, so polling faster buys nothing.
    readonly property int pollSeconds: Math.max(60, Plasmoid.configuration.pollSeconds || 300)
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
    function provStale(name) {
        return provAvailable(name) && !!usage[name].stale;
    }
    function provError(name) {
        return provAvailable(name) ? (usage[name].error || "") : "";
    }
    function titleFor(name) {
        if (name === "openrouter") return "OpenRouter";
        if (name === "agy") return "Gemini";
        return name.charAt(0).toUpperCase() + name.slice(1);
    }
    function anyAvailable() {
        for (var i = 0; i < providerNames.length; i++)
            if (provAvailable(providerNames[i])) return true;
        return false;
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
        // --force bypasses the helper's minimum-interval throttle (not its
        // 429 backoff), for the popup's Refresh button.
        function poll(force) { exec.connectSource(root.helperCmd + (force ? " --force" : "")); }
    }

    function refresh(force) {
        if (!force && Date.now() - lastFetch < 60000) return;
        lastFetch = Date.now();
        exec.poll(!!force);
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

    // Always active: the panel must never collapse the widget just because a
    // fetch failed or a provider is temporarily unavailable.
    Plasmoid.status: PlasmaCore.Types.ActiveStatus

    toolTipMainText: "AI usage"
    toolTipSubText: {
        if (!usage) return "Loading…";
        var parts = [];
        var names = providerNames;
        for (var i = 0; i < names.length; i++) {
            var n = names[i];
            if (!provAvailable(n)) continue;
            var w = provWindow(n);
            var s = titleFor(n) + " " + (w ? Math.round(w.percent) + "%" : "—");
            if (n === "openrouter" && usage[n].balance !== undefined && usage[n].balance !== null)
                s = titleFor(n) + " $" + usage[n].balance.toFixed(2) + " left" + (w ? ", cap " + Math.round(w.percent) + "%" : "");
            if (provError(n)) s += " (" + provError(n) + (w ? ", showing cached" : "") + ")";
            var sp = usage[n].spend;
            if (sp && sp.limitReached) s += " · credits: limit reached";
            parts.push(s);
        }
        return parts.length ? parts.join("   ·   ") : "No providers detected";
    }

    // ---- one clickable panel ring + optional % for a provider ----
    component ProviderChip: MouseArea {
        id: chip
        property string provider: "claude"
        property var win: root.provWindow(provider)
        property var prov: root.usage ? root.usage[provider] : null
        property bool hasError: !!(prov && prov.error)
        property bool stale: root.provStale(provider)

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
            // Cached/stale numbers render dimmed so a stuck fetch is visible
            // without hiding the last known values.
            opacity: (chip.containsMouse ? 0.8 : 1.0) * (chip.stale && chip.win ? 0.55 : 1.0)

            Ring {
                provider: chip.provider
                ringSize: root.ringSize
                Layout.preferredWidth: root.ringSize
                Layout.preferredHeight: root.ringSize
                Layout.alignment: Qt.AlignVCenter
                percent: chip.win ? chip.win.percent : 0
                severity: chip.win ? chip.win.severity : (chip.hasError ? "crit" : "ok")
                error: chip.hasError && !chip.win
            }
            PlasmaComponents.Label {
                // Stay visible whenever the provider exists: a failed fetch with
                // no cached data shows "!" instead of vanishing.
                visible: root.showPercent
                text: {
                    if (chip.provider === "openrouter" && root.orLabel === "dollars"
                            && chip.prov && chip.prov.balance !== undefined && chip.prov.balance !== null) {
                        var b = chip.prov.balance;
                        return "$" + (b < 10 ? b.toFixed(2) : Math.round(b));
                    }
                    return chip.win ? Math.round(chip.win.percent) + "%" : (chip.hasError ? "!" : "—");
                }
                font.bold: true
                color: (chip.hasError && !chip.win) ? Kirigami.Theme.negativeTextColor
                                                    : Kirigami.Theme.textColor
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
        ProviderChip { provider: "agy" }
        ProviderChip { provider: "openrouter" }
        MouseArea {
            visible: !root.anyAvailable()
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
            visible: !!(popup.prov && popup.prov.error)
            text: {
                if (!popup.prov || !popup.prov.error) return "";
                var t = popup.prov.error;
                if (popup.prov.stale && popup.prov.fetchedAt)
                    t += "\nShowing cached data from " + Qt.formatTime(new Date(popup.prov.fetchedAt * 1000));
                return t;
            }
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
                ? "Credits  " + (popup.prov.spend.currency || "") + " "
                  + (popup.prov.spend.used || "—") + " / " + (popup.prov.spend.limit || "—")
                  + (popup.prov.spend.status ? "  · " + popup.prov.spend.status : "")
                : "Credits"
            percent: (popup.prov && popup.prov.spend) ? popup.prov.spend.percent : 0
            severity: (popup.prov && popup.prov.spend) ? popup.prov.spend.severity : "ok"
        }

        PlasmaComponents.Label {
            visible: popup.sp === "openrouter" && !!(popup.prov && popup.prov.balance !== undefined && popup.prov.balance !== null)
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: {
                if (!popup.prov || popup.prov.balance === undefined || popup.prov.balance === null) return "";
                var f = popup.prov.floors || {};
                var sev = popup.prov.balanceSeverity || "ok";
                var t = "Balance $" + popup.prov.balance.toFixed(2);
                if (sev === "crit") t += "  · below $" + f.crit + " floor, top up";
                else if (sev === "warn") t += "  · below $" + f.warn + " floor";
                else if (f.warn !== undefined) t += "  · amber under $" + f.warn + ", red under $" + f.crit;
                return t;
            }
            color: (popup.prov && popup.prov.balanceSeverity === "crit") ? Kirigami.Theme.negativeTextColor
                 : (popup.prov && popup.prov.balanceSeverity === "warn") ? Kirigami.Theme.neutralTextColor
                 : Kirigami.Theme.textColor
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            font.bold: !!(popup.prov && popup.prov.balanceSeverity !== "ok")
        }

        PlasmaComponents.Label {
            visible: !popup.prov || !popup.prov.available
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: root.loadError.length ? root.loadError
                : "No credentials found.\nSign in with the claude or codex CLI,\nor put an OpenRouter key in ~/.config/ai-usage/openrouter.key."
            opacity: 0.8
        }

        Item { Layout.fillWidth: true; height: Kirigami.Units.smallSpacing }

        RowLayout {
            Layout.fillWidth: true
            PlasmaComponents.Label {
                Layout.fillWidth: true
                opacity: 0.6
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                text: {
                    if (popup.prov && popup.prov.fetchedAt)
                        return "Data from " + Qt.formatTime(new Date(popup.prov.fetchedAt * 1000))
                            + (popup.prov.source === "quota.sh" ? " (via crew)" : "");
                    return root.lastUpdated ? "Updated " + Qt.formatTime(new Date(root.lastUpdated)) : "Loading…";
                }
            }
            PlasmaComponents.Button {
                text: "Refresh"
                icon.name: "view-refresh-symbolic"
                onClicked: root.refresh(true)
            }
        }
    }
}
