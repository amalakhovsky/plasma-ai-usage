.pragma library

// Brand base hues (shown when a provider is healthy) and the severity ramp
// toward red. The ramp mirrors the GNOME extension's level palette.
var BRAND = { claude: "#d97757", codex: "#10a37f" };
var SEV   = { ok: null, warn: "#ff7800", crit: "#e01b24" };

// Ring/bar fill color: brand hue at "ok", shifting toward amber then red as
// severity rises.
function rampColor(provider, severity) {
    return SEV[severity] || BRAND[provider] || "#888888";
}

function brand(provider) {
    return BRAND[provider] || "#888888";
}

// The window a panel ring should reflect: the shortest (session/5-hour); falls
// back to whatever exists (e.g. Codex Plus has only a weekly window).
function panelWindow(windows, mode) {
    if (!windows || windows.length === 0)
        return null;
    var sorted = windows.slice().sort(function (a, b) { return a.sortSeconds - b.sortSeconds; });
    if (mode === "weekly") {
        for (var i = sorted.length - 1; i >= 0; i--)
            if (sorted[i].group === "weekly") return sorted[i];
        return sorted[sorted.length - 1];
    }
    if (mode === "max") {
        var top = sorted[0];
        for (var j = 1; j < sorted.length; j++)
            if (sorted[j].percent > top.percent) top = sorted[j];
        return top;
    }
    return sorted[0]; // "shortest" (default)
}

// "resets in 4h 21m" / "resets in 2d 5h" / "resetting…" from an ISO string.
function relativeReset(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var diff = t - Date.now();
    if (diff <= 0) return "resetting…";
    var mins = Math.round(diff / 60000);
    if (mins < 60) return "resets in " + mins + "m";
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return "resets in " + hrs + "h " + (mins % 60) + "m";
    var days = Math.floor(hrs / 24);
    return "resets in " + days + "d " + (hrs % 24) + "h";
}

// Compact magnitude for the panel tooltip: "4h", "2d", "45m", "" when unknown.
function compactReset(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var diff = t - Date.now();
    if (diff <= 0) return "now";
    var mins = Math.round(diff / 60000);
    if (mins < 60) return mins + "m";
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + "h";
    return Math.floor(hrs / 24) + "d";
}
