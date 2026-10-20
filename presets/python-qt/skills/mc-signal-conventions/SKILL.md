---
name: mc-signal-conventions
description: Rules for Qt signals, slots, properties, QML registration and worker threads in mocap-desktop-gui. Use when adding or changing Signal, Slot, Property, QObject subclasses, context properties, McBaseWorker/QThreadPool work, or anything QML binds to in mocap_desktop_gui/.
---

# Signals, properties and threads in mocap-desktop-gui

## How Python reaches QML

- Singletons are exposed as **context properties** in `mocap_desktop_gui/main.py`
  (`engine.rootContext().setContextProperty(...)`): `mcProjectManager`,
  `mcTakeRunsManager`, `mcAppManager`, `mcAppSettings`, `mcVideoManager`,
  `mcVideosUploader`, `mcCloudService`, `FontConfig`, and others. Each is a
  `.Instance()` singleton. Add a new context property only for a new
  app-wide service; otherwise hang new API off the existing owner.
- Types QML instantiates or receives as objects are registered with
  `qmlRegisterType(Cls, QML_IMPORT_NAME, QML_IMPORT_MAJOR_VERSION, 0, "Name")`
  in `main.py` (`McCalib`, `McTake`, `McTakeRun`, the settings classes,
  `McVideoPaintItem`, `McVideoFrameClock`, ...). A new QObject type passed to QML
  as a typed property must be registered there.

## Properties

- `@Property(<type>, notify=<signal>)` for anything QML binds to. The notify
  signal is a class attribute `xxxChanged = Signal()` and is emitted **only when
  the value actually changes**.
- A property without `notify` is constant to QML: the binding never updates.
  Use that only for values that truly never change.
- QObject-valued properties must return objects with a parent (or owned by a
  long-lived Python reference), or QML may see them deleted
  ("Internal C++ object already deleted").

## Signals

- Declare as class attributes with a trailing comment naming the arguments,
  matching the existing style:
  `takeVideoRemoved = Signal(str, str)  # takeId, videoId`
- Signals are the decoupling mechanism: prefer emitting a signal over calling
  methods across subsystem boundaries (project manager ↔ take runs ↔ video ↔
  cloud). Subsystems re-emit child signals rather than letting QML reach deep
  into children (see `projectTakePropsChanged`).
- Use ids (`str` take id, camera id, actor profile id) in signal arguments, not
  Python objects, unless the receiver needs the object and it is a registered
  type.
- The compare viewport uses its own `genQmlAnimationForCompare` signal; never
  reuse the main 3D animation signal for a second viewport.

## Slots

- `@Slot(<arg types>)` / `@Slot(..., result=<type>)` for everything QML calls.
  Keep slot bodies short in `mc_project_manager.py`: validate, delegate to the
  focused module, emit.
- Slots called from QML must not raise: catch the expected `MoveError`s, emit
  the existing error/failed signal for the area, and log.

## Threads

- Long work (video decoding, file parsing, calibration processing, polling,
  uploads) runs off the main thread with `McBaseWorker(fn, *args)` from
  `mc_worker.py` started on `QThreadPool.globalInstance()` (see
  `mc_video_manager.py` and `mc_import_folder.py`).
- `McBaseWorker.signals` has `result(object)`, `error(object)` (a `MoveError`)
  and `finished()`. Connect **all three** where they matter; an unconnected
  `error` is a silent failure.
- The worker function must not touch QObjects that live on the main thread or in
  QML, and must not emit their signals directly. Return a plain result and let
  the `result` handler (main thread) update state and emit.
- Errors: raise `MoveError` with an `MvDesktopErrorCode` from `mc_error_codes.py`;
  add a new code there rather than reusing an unrelated one.

## Where code goes

`mc_project_manager.py` is over 3000 lines and is the wiring layer. New logic
belongs in a focused module (the repo has many: `mc_calibration_library.py`,
`mc_project_calib_apply.py`, `mc_take_settings_action.py`, ...) that the manager
calls. Comments explain why in words; never cite Jira keys in code.
