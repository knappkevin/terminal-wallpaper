import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
import qs.Commons
import qs.Ui

// Terminal Wallpaper: a real VTE terminal (via term-bg.py) over a QML
// wallpaper surface. The wallpaper is rendered in QML exactly like
// omarchy.background (per-screen PanelWindow on the Background layer with a
// 420ms angled reveal), so detection and hot-switching use the same
// IPC-driven mechanism stock does: omarchy-theme-set calls
// `background themeTransition` (or `shell applyTheme` as a fallback) and
// omarchy-theme-bg-set calls `background set`. The thin VTE helper rides on
// the BOTTOM layer above the wallpaper and is nudged (SIGHUP) whenever the
// shell's Color singleton changes -- the same palette event stock's UI
// reacts to. No theme-set hook, no mtime poll, no daemon, no singleton lock.
Item {
  id: root

  // Injected by the shell host.
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null
  property var pluginRegistry: null

  readonly property string home: Quickshell.env("HOME")
  readonly property string stateHome: home + "/.local/state"
  readonly property string currentBackgroundLink: stateHome + "/omarchy/current/background"
  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "github.knappkevin.terminal-wallpaper"
  readonly property string pluginDir: manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : (home + "/.config/omarchy/plugins/" + pluginId)
  readonly property string helperPath: pluginDir + "/term-bg.py"
  readonly property string settingsPath: stateHome + "/omarchy/terminal-wallpaper/terminal-wallpaper.json"

  // ----------------------------------------------------------- wallpaper
  // Ported from omarchy.background: backgroundVersion drives the reveal, the
  // incoming layer grows behind a slanted, feathered mask that sweeps left
  // to right as revealProgress eases 0->1 (420ms InOutCubic).
  property string currentBackground: ""
  property string displayedBackground: ""
  property string incomingBackground: ""
  property string oldBackground: ""
  property bool finishingTransition: false
  property int backgroundVersion: 0
  property int revealStartedVersion: -1
  property int pendingThemeVersion: -1
  property string pendingColorsRaw: ""
  property string pendingShellRaw: ""
  property real revealProgress: 1

  function imageUrl(path) { return Util.fileUrl(path) }

  function refreshBackground() {
    if (!readlinkProc.running) readlinkProc.running = true
  }

  function setBackground(path, instant) {
    transitionBackground("", path, path, instant, false)
  }

  function transitionBackground(fromPath, path, finalPath, instant, force) {
    path = String(path || "").trim()
    finalPath = String(finalPath || path).trim()
    fromPath = String(fromPath || "").trim()
    if (!path || (!force && finalPath === currentBackground)) return
    currentBackground = finalPath
    backgroundVersion += 1
    revealStartedVersion = -1

    revealAnimation.stop()
    finishingTransition = false

    if (instant || !displayedBackground) {
      oldBackground = ""
      incomingBackground = ""
      displayedBackground = path
      revealProgress = 1
      return
    }

    oldBackground = fromPath || displayedBackground
    incomingBackground = path
    revealProgress = 0
  }

  function setPendingTheme(colorsB64, shellB64) {
    pendingColorsRaw = Util.decodeBase64(colorsB64)
    pendingShellRaw = Util.decodeBase64(shellB64)
    pendingThemeVersion = backgroundVersion
    pendingThemeFallbackTimer.restart()
  }

  function applyPendingTheme() {
    // Background polling can advance backgroundVersion while a theme switch is
    // pending; the latest theme payload should still apply.
    if (pendingThemeVersion < 0) return
    pendingThemeFallbackTimer.stop()
    Color.loadColors(pendingColorsRaw)
    // Color.loadShell also refreshes Style so the type scale flips with the
    // background reveal instead of waiting for a separate reload path.
    Color.loadShell(pendingShellRaw)
    Style.scheduleRefresh()
    pendingThemeVersion = -1
    pendingColorsRaw = ""
    pendingShellRaw = ""
  }

  function transitionBackgroundWithTheme(fromPath, path, finalPath, colorsB64, shellB64) {
    transitionBackground(fromPath, path, finalPath, false, true)
    setPendingTheme(colorsB64, shellB64)
    if (!incomingBackground || revealProgress >= 1) applyPendingTheme()
  }

  function startReveal(panel) {
    if (!incomingBackground) return
    panel.maskReady = true
    if (revealStartedVersion === backgroundVersion) return
    revealStartedVersion = backgroundVersion
    applyPendingTheme()
    revealAnimation.restart()
  }

  Process {
    id: readlinkProc
    command: ["readlink", "-f", root.currentBackgroundLink]
    stdout: StdioCollector {
      onStreamFinished: root.setBackground(String(text || "").trim(), false)
    }
  }

  // ----------------------------------------------------------- background IPC
  // Mirrors omarchy.background's handler so omarchy-theme-set /
  // omarchy-theme-bg-set keep working unchanged while omarchy.background is
  // disabled. Owning this target makes us responsible for applying the theme
  // payload (Color.loadColors/loadShell) -- omarchy-theme-set's
  // `background themeTransition || shell applyTheme` fallback no longer fires
  // while we answer, so the whole shell would otherwise not re-tint.
  IpcHandler {
    target: "background"

    function ping(): string { return "ok" }

    function refresh() { root.refreshBackground() }

    function set(path: string) { root.setBackground(path, false) }

    function setInstant(path: string) { root.setBackground(path, true) }

    function transition(fromPath: string, path: string) {
      root.transitionBackground(fromPath, path, path, false, false)
    }

    function themeTransition(fromPath: string, path: string, finalPath: string, colorsB64: string, shellB64: string) {
      root.transitionBackgroundWithTheme(fromPath, path, finalPath, colorsB64, shellB64)
    }
  }

  Timer {
    id: pendingThemeFallbackTimer
    interval: 300
    repeat: false
    onTriggered: root.applyPendingTheme()
  }

  NumberAnimation {
    id: revealAnimation
    target: root
    property: "revealProgress"
    from: 0
    to: 1
    duration: 420
    easing.type: Easing.InOutCubic
    onFinished: {
      if (root.incomingBackground) {
        root.displayedBackground = root.currentBackground || root.incomingBackground
        root.finishingTransition = true
      }
      root.revealProgress = 1
    }
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: panel
      required property var modelData

      screen: modelData
      visible: !remapGuard.remapping
      anchors { top: true; bottom: true; left: true; right: true }

      ScreenMoveRemap {
        id: remapGuard
        window: panel
      }
      color: "transparent"
      // Keep render updates enabled. The background layer has been observed to
      // lose its committed buffer while parked with updatesEnabled=false,
      // leaving a black desktop until omarchy-shell is restarted. The wallpaper
      // itself is static, so this favors correctness over a small render-loop
      // optimization.
      updatesEnabled: true

      property bool maskReady: false

      function maybeStartReveal() {
        if (!root.incomingBackground || root.revealProgress !== 0 || maskReady) return
        if (incomingFrame.status !== Image.Ready) return
        Qt.callLater(function() {
          if (!root.incomingBackground || root.revealProgress !== 0 || maskReady) return
          if (incomingFrame.status !== Image.Ready) return
          root.startReveal(panel)
        })
      }

      WlrLayershell.namespace: "omarchy-terminal-wallpaper"
      WlrLayershell.layer: WlrLayer.Background
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      exclusionMode: ExclusionMode.Ignore

      Image {
        id: base
        anchors.fill: parent
        source: root.imageUrl(root.displayedBackground)
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: true
        onStatusChanged: {
          if (status === Image.Ready && root.finishingTransition) {
            root.incomingBackground = ""
            root.oldBackground = ""
            root.finishingTransition = false
          }
        }
      }

      Image {
        id: oldFrame
        anchors.fill: parent
        source: root.imageUrl(root.oldBackground)
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: false
        smooth: true
        mipmap: true
        visible: root.oldBackground !== "" && root.revealProgress < 1
        onStatusChanged: panel.maybeStartReveal()
      }

      Item {
        id: incomingLayer
        anchors.fill: parent
        visible: root.incomingBackground !== "" && incomingFrame.status === Image.Ready && (root.revealProgress >= 1 || panel.maskReady)
        layer.enabled: root.incomingBackground !== "" && root.revealProgress < 1
        layer.smooth: true
        layer.effect: MultiEffect {
          maskEnabled: true
          maskSource: revealMask
          maskThresholdMin: 0.5
          maskSpreadAtMin: 0.02
        }

        Image {
          id: incomingFrame
          anchors.fill: parent
          source: root.imageUrl(root.incomingBackground)
          fillMode: Image.PreserveAspectCrop
          asynchronous: true
          cache: false
          smooth: true
          mipmap: true
          onStatusChanged: panel.maybeStartReveal()
        }
      }

      Item {
        id: revealMask
        anchors.fill: parent
        visible: false
        layer.enabled: true

        readonly property real slant: -0.18
        readonly property real centerTop: width / 2 - slant * height / 2
        readonly property real centerBottom: width / 2 + slant * height / 2
        readonly property real reach: width / 2 + Math.abs(slant) * height / 2 + 4
        readonly property real spread: reach * root.revealProgress

        Shape {
          anchors.fill: parent
          antialiasing: true
          preferredRendererType: Shape.CurveRenderer
          ShapePath {
            fillColor: "white"
            strokeColor: "transparent"
            startX: revealMask.centerTop - revealMask.spread; startY: 0
            PathLine { x: revealMask.centerTop + revealMask.spread; y: 0 }
            PathLine { x: revealMask.centerBottom + revealMask.spread; y: revealMask.height }
            PathLine { x: revealMask.centerBottom - revealMask.spread; y: revealMask.height }
            PathLine { x: revealMask.centerTop - revealMask.spread; y: 0 }
          }
        }
      }

      Connections {
        target: root
        function onIncomingBackgroundChanged() {
          panel.maskReady = false
          panel.maybeStartReveal()
        }
      }

      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onDoubleClicked: function(mouse) {
          // Single source of config: the menu. Right-click also opens the
          // stock background switcher for convenience.
          if (mouse.button === Qt.RightButton) {
            if (!bgSwitchProc.running) bgSwitchProc.running = true
          } else {
            root.openMenu()
          }
          mouse.accepted = true
        }
      }
    }
  }

  // ------------------------------------------------------------ settings
  readonly property var defaults: ({
    command: "while true; do ttfx -i ~/.config/omarchy/branding/screensaver.txt --frame-rate 60 --canvas-width 0 --canvas-height 0 --reuse-canvas --anchor-canvas c --anchor-text c --random-effect --no-eol --no-restore-cursor; done",
    fontFamily: "monospace",
    fontSize: 14,
    fgColor: "#dddddd",
    bgColor: "#111111",
    bgOpacity: 0.0,
    posX: 0, posY: 0, sizeX: 100, sizeY: 100,
    autoRestart: false,
    cursorVisible: false,
    useTheme: true,
    interactive: false,
    debug: false
  })

  property var cfg: ({})

  function setting(key) {
    var v = cfg[key]
    if (v === undefined || v === null) return defaults[key]
    return v
  }

  function numberSetting(key) {
    var n = Number(setting(key))
    return isFinite(n) ? n : Number(defaults[key])
  }

  function mergedConfig() {
    var out = {}
    for (var k in defaults) out[k] = defaults[k]
    for (var k in cfg) out[k] = cfg[k]
    return out
  }

  function parseSettings(raw) {
    try {
      var d = JSON.parse(String(raw || "{}"))
      if (d && typeof d === "object") { cfg = d; return }
    } catch (e) {}
    cfg = ({})
  }

  function writeSettings(obj) {
    var json = JSON.stringify(obj || mergedConfig(), null, 2)
    writeProc.command = [
      "bash", "-lc",
      "mkdir -p " + Util.shellQuote(home + "/.local/state/omarchy/terminal-wallpaper")
      + " && printf '%s' " + Util.shellQuote(json)
      + " > " + Util.shellQuote(settingsPath)
    ]
    writeProc.running = true
  }

  property Process writeProc: Process { id: writeProc }

  FileView {
    id: settingsFile
    path: root.settingsPath
    watchChanges: true
    printErrors: false
    onLoaded: { root.parseSettings(text()); root.nudgeHelper() }
    onFileChanged: reload()
    onLoadFailed: root.writeSettings(null)
  }

  // ------------------------------------------------------------- helper
  // term-bg.py is a plain child of omarchy-shell: respawned on exit, never
  // daemonized. A shell restart briefly hides the terminal (never the
  // wallpaper, which is QML now) and the next spawn brings it back.
  property bool active: true
  property bool depsOk: true

  Process {
    id: helperProc
    command: ["python3", root.helperPath, "--config", root.settingsPath].concat(root.setting("debug") ? ["--debug"] : [])
    stdout: StdioCollector {}
    stderr: StdioCollector {
      onStreamFinished: if (text && text.trim().length) root.logLine("helper stderr: " + text.trim())
    }
    onExited: function(exitCode, exitStatus) {
      root.logLine("helper exited code=" + exitCode + " status=" + exitStatus)
      if (root.active) helperRestart.restart()
    }
  }

  Timer {
    id: helperRestart
    interval: 1200
    onTriggered: if (root.active && !helperProc.running) helperProc.running = true
  }

  function nudgeHelper() {
    if (!root.active) return
    if (!helperProc.running) { helperProc.running = true; return }
    // SIGHUP makes the running helper re-read settings + colors.toml in
    // place. A full restart would tear the BOTTOM-layer surface down and
    // black-flash the terminal; only a crash should do that.
    helperProc.signal(1) // SIGHUP
  }

  function startHelper() {
    if (!root.active) return
    if (!helperProc.running) helperProc.running = true
  }

  function restartPlugin() {
    // Full helper restart: SIGKILL -> onExited -> helperRestart -> respawn.
    // Rebuilds the layer surfaces and re-runs the command from scratch (the
    // recovery path for a wedged terminal/child).
    if (!root.active) return
    if (helperProc.running) helperProc.signal(9)
    else helperProc.running = true
    // Also restart the menu: tear it down and rebuild it so the drafts
    // re-read from settings, scroll resets, and keyboard focus is re-taken
    // (recovers a wedged menu / stale drafts / lost focus).
    var wasOpen = root.menuOpen
    root.closeMenu()
    flick.contentY = 0
    if (wasOpen) Qt.callLater(function() { root.openMenu() })
  }

  Process {
    id: depCheck
    command: ["python3", "-c", "import gi; gi.require_version('Vte','2.91'); gi.require_version('GtkLayerShell','0.1')"]
    onExited: function(code) { root.depsOk = (code === 0) }
  }

  // Change-wallpaper button: reuses the stock background switcher. The result
  // routes back through the `background set` IPC -> our handler -> reveal.
  Process {
    id: bgSwitchProc
    command: ["bash", "-c", "background=$(omarchy-theme-bg-switcher); [[ -n $background ]] && omarchy-theme-bg-set \"$background\""]
    onExited: root.refreshBackground()
  }

  // Re-apply the terminal palette whenever the shell's Color singleton
  // changes. Color.loadColors fires from `shell applyTheme` -- the exact IPC
  // stock's UI reacts to -- so the wallpaper terminal tracks the same event.
  // (When we own the `background` target, applyTheme runs from our
  // themeTransition handler via Color.loadColors, so this still fires.)
  Connections {
    target: Color
    function onForegroundChanged() { root.nudgeHelper() }
    function onBackgroundChanged() { root.nudgeHelper() }
    function onAccentChanged() { root.nudgeHelper() }
    function onUrgentChanged() { root.nudgeHelper() }
    function onMutedChanged() { root.nudgeHelper() }
  }

  Component.onCompleted: {
    depCheck.running = true
    startHelper()
    refreshBackground()
  }

  Component.onDestruction: {
    root.active = false
    helperRestart.stop()
    if (helperProc.running) helperProc.running = false
  }

  // ----------------------------------------------------------- death log
  Process { id: exitLogProc; command: [] }

  function logLine(msg: string) {
    // Opt-in like the helper's breadcrumb trail: only write the exits/stderr
    // log when debug is enabled. Default installs leave no log files behind.
    if (root.setting("debug") !== true) return
    if (exitLogProc.running) return
    exitLogProc.command = ["bash", "-c",
      'printf "%s %s\\n" "$(date +%s)" "$1" >> "$2"',
      "-", msg, home + "/.local/state/terminal-wallpaper-exits.log"]
    exitLogProc.running = true
  }

  // ------------------------------------------------------------ menu state
  property bool menuOpen: false
  property string draftCommand: ""
  property real draftFontSize: 14
  property real draftOpacity: 0.9
  property string draftFg: "#cacccc"
  property string draftBg: "#101315"
  property real draftPosX: 0
  property real draftPosY: 0
  property real draftSizeX: 100
  property real draftSizeY: 100
  property bool draftAutoRestart: true
  property bool draftUseTheme: true
  property bool draftInteractive: true

  function beginEdit() {
    draftCommand = String(setting("command"))
    draftFontSize = numberSetting("fontSize")
    draftOpacity = numberSetting("bgOpacity")
    draftFg = String(setting("fgColor"))
    draftBg = String(setting("bgColor"))
    draftPosX = numberSetting("posX")
    draftPosY = numberSetting("posY")
    draftSizeX = numberSetting("sizeX")
    draftSizeY = numberSetting("sizeY")
    draftAutoRestart = setting("autoRestart") === true
    draftUseTheme = setting("useTheme") !== false
    draftInteractive = setting("interactive") !== false
  }

  function openMenu() {
    beginEdit()
    menuOpen = true
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function closeMenu() {
    menuOpen = false
  }

  function commit() {
    // Persist the current draft values immediately. writeSettings ->
    // FileView onFileChanged -> nudgeHelper (SIGHUP) -> the helper re-applies
    // in place, so every control can live-apply without an explicit Apply button.
    var next = mergedConfig()
    next.command = draftCommand
    next.fontSize = draftFontSize
    next.bgOpacity = draftOpacity
    next.fgColor = draftFg
    next.bgColor = draftBg
    next.posX = draftPosX
    next.posY = draftPosY
    next.sizeX = draftSizeX
    next.sizeY = draftSizeY
    next.autoRestart = draftAutoRestart
    next.useTheme = draftUseTheme
    next.interactive = draftInteractive
    writeSettings(next)
  }

  function geometryPreset(name) {
    if (name === "top") return { x: 0, y: 0, w: 100, h: 50 }
    if (name === "bottom") return { x: 0, y: 50, w: 100, h: 50 }
    if (name === "center") return { x: 20, y: 20, w: 60, h: 60 }
    return { x: 0, y: 0, w: 100, h: 100 } // fullscreen
  }

  function applyGeometry(name) {
    var p = geometryPreset(name)
    draftPosX = p.x
    draftPosY = p.y
    draftSizeX = p.w
    draftSizeY = p.h
  }

  function isGeometry(name) {
    var p = geometryPreset(name)
    return draftPosX === p.x && draftPosY === p.y && draftSizeX === p.w && draftSizeY === p.h
  }

  // Nudge step as % of the bar-free screen area (1% ≈ 19px on a 1920px
  // display). posX/posY are clamped so the terminal can't be dragged off
  // screen: the far edge stops at the terminal's far edge.
  readonly property real nudgeStep: 1
  readonly property real nudgeBtn: Style.space(32)

  function nudge(dx, dy) {
    var maxX = Math.max(0, 100 - draftSizeX)
    var maxY = Math.max(0, 100 - draftSizeY)
    draftPosX = Math.max(0, Math.min(maxX, draftPosX + dx))
    draftPosY = Math.max(0, Math.min(maxY, draftPosY + dy))
    commit()
  }

  function resetPosition() {
    draftPosX = Math.max(0, (100 - draftSizeX) / 2)
    draftPosY = Math.max(0, (100 - draftSizeY) / 2)
    commit()
  }

  function setSize(w, h) {
    draftSizeX = Math.max(10, Math.min(100, Math.round(w)))
    draftSizeY = Math.max(10, Math.min(100, Math.round(h)))
    // Keep the terminal on-screen: pull position back in if enlarging
    // would push the far edge past the display.
    draftPosX = Math.min(draftPosX, 100 - draftSizeX)
    draftPosY = Math.min(draftPosY, 100 - draftSizeY)
    commit()
  }

  // ----------------------------------------------------------------- IPC
  IpcHandler {
    target: "terminal-wallpaper"

    function open() { root.openMenu() }
    function close() { root.closeMenu() }
    function toggle() { root.menuOpen ? root.closeMenu() : root.openMenu() }
    function refresh() { root.nudgeHelper() }
    function setCommand(command: string) {
      var next = root.mergedConfig()
      next.command = command
      root.writeSettings(next)
    }
    function status(): string {
      return JSON.stringify({
        command: String(root.setting("command")),
        helper: helperProc.running,
        deps: root.depsOk
      })
    }
  }

  // ----------------------------------------------------------- config menu
  PanelWindow {
    id: menuWin
    visible: root.menuOpen
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore

    WlrLayershell.namespace: "omarchy-terminal-wallpaper-menu"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.menuOpen ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim
      MouseArea {
        anchors.fill: parent
        onClicked: root.closeMenu()
      }
    }

    BorderSurface {
      id: card
      width: Math.min(parent.width - Style.space(64), Style.space(460))
      height: Math.min(parent.height - Style.space(48), contentColumn.implicitHeight + card.contentTopInset + card.contentBottomInset)
      anchors.centerIn: parent
      color: Color.menu.background
      borderSpec: Border.localOrSurfaceSpec("menu", "border", Color.menu.border, Color.menu.border, Math.max(1, Style.space(2)))
      radius: Style.cornerRadius
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: function(mouse) { mouse.accepted = true } }

      Item {
        id: keyCatcher
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        focus: true

        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) {
            root.closeMenu()
            event.accepted = true
          }
        }

        Flickable {
          id: flick
          anchors.fill: parent
          contentWidth: contentColumn.width
          contentHeight: contentColumn.height
          clip: true
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: contentColumn
            width: flick.width
            spacing: Style.spacing.lg

            Text {
              text: "Terminal Wallpaper"
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.heading
              font.bold: true
            }

            Text {
              visible: !root.depsOk
              text: "Missing dependencies. Run: omarchy pkg add vte3 gtk-layer-shell python-gobject"
              color: Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              width: parent.width
            }

            Column {
              width: parent.width
              spacing: Style.spacing.xs
              Text {
                text: "Command"
                color: Color.menu.text
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
              TextField {
                id: commandField
                width: parent.width
                text: root.draftCommand
                foreground: Color.menu.text
                onTextEdited: root.draftCommand = text
                onEditingFinished: root.commit()
              }
            }

            Column {
              width: parent.width
              spacing: Style.spacing.xs
              Toggle {
                label: "System theme colors"
                description: "Uses colors from the current theme's colors.toml."
                checked: root.draftUseTheme
                foreground: Color.menu.text
                width: parent.width
                onClicked: { root.draftUseTheme = !root.draftUseTheme; root.commit() }
              }
              Toggle {
                label: "Clickable terminal"
                description: "Let clicks and scroll reach the TUI, e.g. btop."
                checked: root.draftInteractive
                foreground: Color.menu.text
                width: parent.width
                onClicked: { root.draftInteractive = !root.draftInteractive; root.commit() }
              }
            }

            Row {
              visible: !root.draftUseTheme
              width: parent.width
              spacing: Style.spacing.md
              Column {
                width: (parent.width - Style.spacing.md) / 2
                spacing: Style.spacing.xs
                Text {
                  text: "Foreground"
                  color: root.draftUseTheme ? Color.muted : Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                TextField {
                  width: parent.width
                  text: root.draftFg
                  enabled: !root.draftUseTheme
                  foreground: Color.menu.text
                  onTextEdited: root.draftFg = text
                  onEditingFinished: root.commit()
                }
              }
              Column {
                width: (parent.width - Style.spacing.md) / 2
                spacing: Style.spacing.xs
                Text {
                  text: "Background"
                  color: root.draftUseTheme ? Color.muted : Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                TextField {
                  width: parent.width
                  text: root.draftBg
                  enabled: !root.draftUseTheme
                  foreground: Color.menu.text
                  onTextEdited: root.draftBg = text
                  onEditingFinished: root.commit()
                }
              }
            }

            Column {
              width: parent.width
              spacing: Style.spacing.xs
              Text {
                text: "Font size: " + Math.round(root.draftFontSize)
                color: Color.menu.text
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
              Item {
                width: parent.width
                implicitHeight: fontSlider.implicitHeight
                PanelSlider {
                  id: fontSlider
                  width: parent.width
                  value: root.draftFontSize
                  minimum: 8
                  maximum: 32
                  integer: true
                  fillColor: Color.menu.text
                  onMoved: function(v) { root.draftFontSize = v }
                  onReleased: function(v) { root.draftFontSize = v; root.commit() }
                }
                MouseArea {
                  anchors.fill: parent
                  acceptedButtons: Qt.NoButton
                  onWheel: function(w) {
                    flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, flick.contentY - w.angleDelta.y))
                    w.accepted = true
                  }
                }
              }
            }

            Column {
              width: parent.width
              spacing: Style.spacing.xs
              Text {
                text: "Background opacity: " + Math.round(root.draftOpacity * 100) + "%"
                color: Color.menu.text
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
              Item {
                width: parent.width
                implicitHeight: opacitySlider.implicitHeight
                PanelSlider {
                  id: opacitySlider
                  width: parent.width
                  value: root.draftOpacity
                  minimum: 0
                  maximum: 1
                  step: 0.05
                  fillColor: Color.menu.text
                  onMoved: function(v) { root.draftOpacity = v }
                  onReleased: function(v) { root.draftOpacity = v; root.commit() }
                }
                MouseArea {
                  anchors.fill: parent
                  acceptedButtons: Qt.NoButton
                  onWheel: function(w) {
                    flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, flick.contentY - w.angleDelta.y))
                    w.accepted = true
                  }
                }
              }
            }

            Column {
              width: parent.width
              spacing: Style.spacing.xs
              Text {
                text: "Layout preset"
                color: Color.menu.text
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
              Row {
                width: parent.width
                spacing: Style.spacing.sm
                Button {
                  text: "Fullscreen"
                  selected: root.isGeometry("fullscreen")
                  foreground: Color.menu.text
                  onClicked: { root.applyGeometry("fullscreen"); root.commit() }
                }
                Button {
                  text: "Top"
                  selected: root.isGeometry("top")
                  foreground: Color.menu.text
                  onClicked: { root.applyGeometry("top"); root.commit() }
                }
                Button {
                  text: "Bottom"
                  selected: root.isGeometry("bottom")
                  foreground: Color.menu.text
                  onClicked: { root.applyGeometry("bottom"); root.commit() }
                }
                Button {
                  text: "Center"
                  selected: root.isGeometry("center")
                  foreground: Color.menu.text
                  onClicked: { root.applyGeometry("center"); root.commit() }
                }
              }
            }

            Row {
              width: parent.width
              spacing: Style.spacing.md

              Column {
                width: (parent.width - Style.spacing.md) / 2
                spacing: Style.spacing.xs
                Text {
                  text: "Adjust position"
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                Item {
                  width: root.nudgeBtn * 2 + Style.space(40)
                  height: root.nudgeBtn * 2 + Style.space(40)
                  anchors.horizontalCenter: parent.horizontalCenter

                  Button {
                    iconText: "󰁝"
                    width: root.nudgeBtn; height: root.nudgeBtn
                    horizontalPadding: 0; verticalPadding: 0
                    foreground: Color.menu.text
                    tooltipText: "Move up"
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    onClicked: root.nudge(0, -root.nudgeStep)
                  }
                  Button {
                    iconText: "󰁍"
                    width: root.nudgeBtn; height: root.nudgeBtn
                    horizontalPadding: 0; verticalPadding: 0
                    foreground: Color.menu.text
                    tooltipText: "Move left"
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left
                    onClicked: root.nudge(-root.nudgeStep, 0)
                  }
                  Button {
                    iconText: "󰁔"
                    width: root.nudgeBtn; height: root.nudgeBtn
                    horizontalPadding: 0; verticalPadding: 0
                    foreground: Color.menu.text
                    tooltipText: "Move right"
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.right: parent.right
                    onClicked: root.nudge(root.nudgeStep, 0)
                  }
                  Button {
                    iconText: "󰁅"
                    width: root.nudgeBtn; height: root.nudgeBtn
                    horizontalPadding: 0; verticalPadding: 0
                    foreground: Color.menu.text
                    tooltipText: "Move down"
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    onClicked: root.nudge(0, root.nudgeStep)
                  }
                }
                Button {
                  anchors.horizontalCenter: parent.horizontalCenter
                  text: "Center position"
                  foreground: Color.menu.text
                  tooltipText: "Center the terminal within the available area"
                  onClicked: root.resetPosition()
                }
              }

              Column {
                width: (parent.width - Style.spacing.md) / 2
                spacing: Style.spacing.xs
                Text {
                  text: "Size"
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                Text {
                  text: "Width: " + Math.round(root.draftSizeX) + "%"
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                Item {
                  width: parent.width
                  implicitHeight: sizeXSlider.implicitHeight
                  PanelSlider {
                    id: sizeXSlider
                    width: parent.width
                    value: root.draftSizeX
                    minimum: 10
                    maximum: 100
                    integer: true
                    fillColor: Color.menu.text
                    onMoved: function(v) { root.draftSizeX = v }
                    onReleased: function(v) { root.setSize(v, root.draftSizeY) }
                  }
                  MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.NoButton
                    onWheel: function(w) {
                      flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, flick.contentY - w.angleDelta.y))
                      w.accepted = true
                    }
                  }
                }
                Text {
                  text: "Height: " + Math.round(root.draftSizeY) + "%"
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                Item {
                  width: parent.width
                  implicitHeight: sizeYSlider.implicitHeight
                  PanelSlider {
                    id: sizeYSlider
                    width: parent.width
                    value: root.draftSizeY
                    minimum: 10
                    maximum: 100
                    integer: true
                    fillColor: Color.menu.text
                    onMoved: function(v) { root.draftSizeY = v }
                    onReleased: function(v) { root.setSize(root.draftSizeX, v) }
                  }
                  MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.NoButton
                    onWheel: function(w) {
                      flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, flick.contentY - w.angleDelta.y))
                      w.accepted = true
                    }
                  }
                }
              }
            }

            Button {
              text: "Change wallpaper"
              foreground: Color.menu.text
              leftAlign: true
              onClicked: if (!bgSwitchProc.running) bgSwitchProc.running = true
            }

            Item {
              width: parent.width
              implicitHeight: Style.spacing.xxxl + actionRow.implicitHeight
              Row {
                id: actionRow
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                spacing: Style.spacing.md
                Button {
                  text: "Restart"
                  foreground: Color.menu.text
                  onClicked: root.restartPlugin()
                }
                Button {
                  text: "Close"
                  foreground: Color.menu.text
                  onClicked: root.closeMenu()
                }
              }
            }
          }
        }
      }
    }
  }
}
