import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: page

    property alias cfg_pollSeconds: pollSpin.value
    property string cfg_panelWindow: "shortest"
    property alias cfg_showPercent: pctCheck.checked
    property alias cfg_ringSize: sizeSpin.value
    property alias cfg_orWarnUsd: orWarn.value
    property alias cfg_orCritUsd: orCrit.value
    property string cfg_orLabel: "dollars"
    property alias cfg_orNotify: orNotify.checked

    QQC2.SpinBox {
        id: pollSpin
        Kirigami.FormData.label: "Refresh every (seconds):"
        from: 60
        to: 3600
        stepSize: 60
    }

    QQC2.ComboBox {
        id: windowCombo
        Kirigami.FormData.label: "Panel ring shows:"
        textRole: "text"
        valueRole: "value"
        model: [
            { text: "Shortest window (5-hour)", value: "shortest" },
            { text: "Weekly window", value: "weekly" },
            { text: "Most-used window", value: "max" }
        ]
        onActivated: page.cfg_panelWindow = currentValue
        Component.onCompleted: currentIndex = indexOfValue(page.cfg_panelWindow)
    }

    QQC2.CheckBox {
        id: pctCheck
        Kirigami.FormData.label: "Show percentage:"
        text: "next to each ring"
    }

    QQC2.SpinBox {
        id: sizeSpin
        Kirigami.FormData.label: "Ring size (px):"
        from: 14
        to: 34
    }

    Kirigami.Separator {
        Kirigami.FormData.isSection: true
        Kirigami.FormData.label: "OpenRouter"
    }

    QQC2.SpinBox {
        id: orWarn
        Kirigami.FormData.label: "Amber when balance below ($):"
        from: 0
        to: 1000
    }
    QQC2.SpinBox {
        id: orCrit
        Kirigami.FormData.label: "Red when balance below ($):"
        from: 0
        to: 1000
    }
    QQC2.ComboBox {
        id: orLabelCombo
        Kirigami.FormData.label: "Label next to ring:"
        textRole: "text"
        valueRole: "value"
        model: [
            { text: "Dollars left on the account", value: "dollars" },
            { text: "Percent of the key's monthly cap", value: "percent" }
        ]
        onActivated: page.cfg_orLabel = currentValue
        Component.onCompleted: currentIndex = Math.max(0, indexOfValue(page.cfg_orLabel))
    }
    QQC2.CheckBox {
        id: orNotify
        Kirigami.FormData.label: "Notify:"
        text: "desktop notification when the balance crosses a floor"
    }
    QQC2.Label {
        text: "Key file: ~/.config/ai-usage/openrouter.key (one line, chmod 600). The ring appears once the file exists."
        font: Kirigami.Theme.smallFont
        opacity: 0.7
        wrapMode: Text.WordWrap
    }
}
