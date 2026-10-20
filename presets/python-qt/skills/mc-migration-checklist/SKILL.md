---
name: mc-migration-checklist
description: The ~/moveai on-disk layout and the checklist for changing stored formats in mocap-desktop-gui (calibration, camera models, actor profiles, project and take settings, migrations). Use when designing, building or reviewing anything that reads or writes project files, global stores or calibration data.
---

# Stored data and migrations

## Layout (runtime data, outside the repo)

```
~/moveai/
  actor_profiles/          global actor profile store (source of truth)
  calibration/             global calibration library
  camera_models/           global camera models (YAML)
  <licence>.csv / .json    online-token CSV / offline licence JSON
  projects/
    .logs/move_desktop_<date>_<time>.log
    <project>/
      calib/               unified calibration: Intrinsic/ and Extrinsic/ subfolders, YAML
        .meta/calib_meta.json                 the active set
        Extrinsic/<id>/.meta/calib_meta.json  the set that produced <id>
      calibs/              LEGACY (calib_<n>/ .pkl sets + activeCalibName): read once to migrate, never written
      takes/.take_<n>/.runs_local/run_<timestamp>/{log,out_calib,...}
      actor_profiles/      LEGACY per-project profiles: kept and read, never written
```

Path helpers live in `mc_common_paths.py`; never build these paths by hand.

## Rules already decided

- **Global first**: new actor profiles, calibrations and camera models are
  written to the global stores. Legacy per-project copies are read, never
  written, and a local item whose id matches a global one is hidden.
- **Delete archives**: deleting from a library archives; files and references
  stay, and takes that used the item show its name with "deleted".
- **Unified calibration**: intrinsics and extrinsics live under
  `<project>/calib/{Intrinsic,Extrinsic}` as YAML with `sourceApp`,
  `sourceTakeId`, `sourceTakeName` and `timestamp`; `calib_quality` and
  `visualization` nest inside the extrinsic folder. Folder names carry no `post`
  prefix and no hyphens in timestamps.
- **Migration on open**: legacy data migrates when a project is opened, only
  when the same calibration isn't already in the new location.
- **Lifecycle locks**: a successfully processed actor profile take can't be
  reprocessed and its settings are locked.
- **Model parity**: M1/M2/M3 take settings differ (M3 uses
  `OpsVideoPreparationSettings`); allowed operations come from the sibling
  `mocap-settings` repo. Don't add an app-side workaround for one model that
  the others don't get.

Existing migration code to follow: `mc_calib_structure_migration.py`,
`mc_camera_models_migration.py`, the migration paths in
`mc_project_manager.py` (grep `migrat`) and their tests
(`test_mc_calib_structure_migration.py`, `test_mc_camera_models_migration.py`,
`test_mc_project_manager_calib_migration.py`).

## Checklist for any change to a stored format

1. **Inventory**: list every reader and writer of the format (grep the file
   name, keys and YAML attributes). All of them change together.
2. **Backward compatible read**: files written by the previous release load.
   New fields have defaults; removed fields are ignored, not fatal.
3. **Forward safety**: the new code never writes something the previous release
   would crash on, unless the spec accepts that and it is called out for the
   PR.
4. **Migration**: runs once, at the existing trigger (project open), is
   idempotent, skips items already migrated, never deletes the legacy source,
   and logs what it moved.
5. **Atomic writes**: write to a temp file in the same folder, then
   `os.replace`. No partially written YAML/JSON on exception.
6. **No silent loss**: an unreadable file is reported (log plus UI error where
   the user is affected) and left in place, never overwritten with defaults.
7. **Links stay valid**: take ↔ calibration ids, provenance attributes, actor
   profile ↔ take links, archived items.
8. **Tests**: a fixture in the old format, a test that it loads, a test that the
   migration produces the new layout, and a test that running it twice changes
   nothing. Use `tmp_path` and patched path helpers, never the real `~/moveai`.
9. **PR note**: describe the format change and the migration for QA, and say
   whether a downgrade is safe.
