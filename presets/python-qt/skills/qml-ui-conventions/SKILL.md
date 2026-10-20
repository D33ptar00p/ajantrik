---
name: qml-ui-conventions
description: UI copy, layout and behaviour rules for QML in mocap-desktop-gui, plus the offscreen screenshot harness for checking QML headlessly. Use when writing, reviewing or screenshotting anything under pyside_rc/qml or its resources.
---

# QML UI conventions and visual checking

## Where things are

- QML in `pyside_rc/qml/` (Material style), icons in `pyside_rc/icons/`,
  registered in `pyside_rc/rc_qml.qrc` / `rc_icons.qrc`. Compiled into
  `mocap_desktop_gui/gen/rc_*.py`; regenerate after any `.qml`/`.qrc` change:
  `$HOME/.virtualenvs/mocap-desktop-gui/bin/python pyside_rc/dev_tool.py`.
- Python objects arrive as context properties (`mcProjectManager`, `mcAppSettings`,
  `FontConfig`, ...) and registered types. Font sizes come from `FontConfig` and
  the `gFontPointSize*` globals in `Main.qml`; don't hardcode pixel sizes where
  neighbours use them.

## Rules the user applies in review

- **No em dashes** in user-facing text; use a hyphen.
- A **blocked action is greyed out and non-clickable with a tooltip** that says
  what to do to unblock it ("Reprocess the actor profile"), not clickable and
  then an error dialog.
- **Truncated text gets a hover tooltip** with the full value (long take,
  profile, camera names in dropdowns and lists).
- **Match the Figma file and existing dialogs**: same button sizes, same corner
  rounding on all four corners, the standard Done button, consistent padding
  above the heading and below the footer row. Prefer an icon button in a corner
  over a wide labelled button when the design shows one.
- **Modest font sizes**: oversized text forces scrollbars on tall screens.
- **Drop redundant helper text** when a tooltip or badge already says it.
- Settings menus nest by meaning (e.g. Mocap settings and Output formats under
  Action Takes), indented consistently with siblings.
- Long lists must not push dialog buttons off-screen: lists scroll, footers
  stay visible.

## Offscreen screenshot harness

`qmllint` catches syntax only. Layout bugs and binding errors show up only at
runtime, so render the real app offscreen and drive it from Python. Write the
script outside the repo (the job folder or a scratch dir) and run it with the
venv python.

1. Set `QT_QPA_PLATFORM=offscreen` and `IS_MOVE_DESKTOP_DEV=1` before importing
   the app. `main.py` runs argparse on `sys.argv`: pass inputs through env vars
   and set `sys.argv = sys.argv[:1]`.
2. Patch **`QGuiApplication.exec`** (main.py builds a `QGuiApplication`). Keep the
   original and call it with no args. In the patch:
   `QTimer.singleShot(6000, probe)` then `return real_exec()`. Six seconds clears
   the 2.5 s splash so the window has a real size.
3. In `probe()`, get the window with
   `EngineManager.get_instance().rootObjects()[0]`.
4. Evaluate QML with
   `QQmlExpression(QQmlEngine.contextForObject(appWindow), appWindow, src)`.
   Use `contextForObject`, not `engine.rootContext()`: ids and globals from
   Main.qml (`appWindow`, `gFontPointSize*`, `globalWarningDialog`) live there.
5. Create dialogs **inside QML**:
   `Qt.createComponent("qrc:/...").createObject(appWindow)`. Objects created from
   Python with `QQmlComponent.create()` get garbage-collected mid-run. QML objects
   reject ad-hoc properties, so don't stash them on the window.
6. `onOpened` fires after the open transition: wait about 1.5 s before
   screenshotting or asserting on what it populates.
7. To open a `Popup` declared inside a component, find it with
   `item.findChildren(QObject)` where `"Popup" in obj.metaObject().className()`
   and set `visible` to true.
8. Screenshot with `appWindow.grabWindow().save(path)`, then quit the app.
   Write results to a file, not stdout (a timed-out process loses buffered
   output). Run with a `timeout` of about 60 s.
9. The offscreen window stays 800x800 (`resize()` is ignored): very wide dialogs
   clip there because of the harness, not the change.
10. Grep the output for the changed file names with `recursive rearrange`,
    `anchors on an item that is managed by a layout`, `ReferenceError`,
    `TypeError` and `Binding loop`.

Run the harness with an isolated `HOME` (a scratch folder) so the app can't
read or migrate the user's real `~/moveai` projects.
