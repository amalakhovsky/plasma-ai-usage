import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: page

    property alias cfg_pollSeconds: pollSpin.value
    property string cfg_panelWindow: "shortest"
    property alias cfg_showPercent: pctCheck.checked
    property alias cfg_ringSize: sizeSpin.value

    QQC2.SpinBox {
        id: pollSpin
        Kirigami.FormData.label: "Refresh every (seconds):"
        from: 30
        to: 3600
        stepSize: 30
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
}
