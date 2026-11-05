# Changelog

## 0.7.0

- Requires serum2-preset-loader `>=0.6,<0.7`, and refuses to start with an older one.
- MPE is off in the state by default, as with a fresh Serum instance. A preset file's MPE settings are left out, as in Serum's editor load.
- New: `--keep-mpe` (`RenderConfig(keep_mpe=True)`) restores the file's MPE settings, for MIDI files written for MPE. A run with it records `"keep_mpe": true` on each Serum 2 result and provenance line.
- A bad FX entry in a preset now fails that job with `InvalidFXTypeError` instead of reaching Serum.
- Provenance logs name the new loader version.

Upgrading: single notes and MIDI files on one channel rendered the same as 0.6.0 in the check for this release (100 presets, single notes; 28 MPE-on presets, MIDI on one channel). MIDI files that send on member channels can sound different on MPE-on presets that respond to them, mostly through channel pressure. Use `--keep-mpe` to get the 0.6.0 result for those.
