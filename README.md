# openrecon-sct

[OpenRecon](https://www.siemens-healthineers.com/magnetic-resonance-imaging/options-and-upgrades/clinical-applications/openrecon) app to run [Spinal Cord Toolbox (SCT)](https://spinalcordtoolbox.com) directly on Siemens MR scanners.

Based on [neuropoly/openrecon-template](https://github.com/neuropoly/openrecon-template), with inspiration from [benoitberanger/openrecon-ants](https://github.com/benoitberanger/openrecon-ants).

> [!WARNING]
> Research use only. Not for diagnostic use.

## Features

| UI parameter | Values | Default |
|---|---|---|
| **SCT command** | `sct_deepseg spinalcord` (contrast-agnostic spinal cord segmentation) | `sct_deepseg spinalcord` |
| **Save original images** | send back the original images (no suffix) | `true` |
| **Save overlay** | send back the original images with the cord contour burned in | `true` |

Output series, for each incoming magnitude series:

| Series | `SequenceDescriptionAdditional` | Content |
|---|---|---|
| original (optional) | – | unchanged input images |
| segmentation | `SCTSeg` | binary spinal cord mask (displayed bright) |
| overlay (optional) | `SCTOverlay` | input images + segmentation contour |

Non-magnitude images (e.g. phase) are passed through unchanged.

### How it works

[app/or-sct.py](app/or-sct.py):

1. Stacks the 2D MRD images into a 3D volume (slices sorted along the slice direction) and builds the NIfTI affine from the MRD geometry (`position`, `read_dir`, `phase_dir`, `slice_dir`, FOV, matrix).
2. Runs the SCT command (`sct_deepseg spinalcord -i ... -o ...`) as a subprocess. SCT lives in its own conda env in the container (`/opt/sct`).
3. Reads the result back, restores the original image order, and sends new MRD series with the original headers.

To add an SCT command: add a choice in [app/or-sct_json_ui.json](app/or-sct_json_ui.json) (`SCTCommand` parameter) and map it in `SCT_COMMANDS` / `process_image()` in [app/or-sct.py](app/or-sct.py).

## Build

Requirements:
- Python ≥ 3.10 with `jsonschema` (`pip install jsonschema`)
- `git`, `zip`, Docker

```bash
python build.py
```

Options:
- `--sct-version 7.3`: SCT release (git tag) installed in the image
- `--skip-docker-version-check`: build even with Docker ≥ 25. Siemens documents a maximum Docker version (`docker save` format changed in Docker 25), so check that the resulting image loads on your scanner software version.

The image is always built for `linux/amd64` (the MR host architecture), including on Apple Silicon (slow, via emulation). SCT and the deep learning model are installed at build time because the scanner has no internet access. The image is large (several GB) because of SCT + PyTorch (CPU).

Output: `build/OpenRecon_NeuroPoly_SCT_V<version>.zip`. Upload that file to the scanner.

## Offline test and dev

Requirements: SCT installed locally (`sct_deepseg` in the `PATH`, or `SCT_DIR` set), plus:

```bash
pip install ismrmrd pydicom pynetdicom nibabel h5py
```

Then follow the [openrecon-template](https://github.com/neuropoly/openrecon-template#how-to-test-locally-the-reconstruction) guidelines: symlink `app/or-sct.py` into `python-ismrmrd-server/`, start `main.py`, and send data with [reco_and_visu.sh](reco_and_visu.sh) (config `or-sct`).

Note: the local `dicom2mrd.py` converter stores the DICOM `ImagePositionPatient` (slice corner) in `position`, while MRD defines it as the slice center. This only shifts the NIfTI origin by half a FOV and does not affect the segmentation.

## TODO

- Test on a scanner (XA60/XA61)
- Expose `sct_deepseg` options (`-thr`, `-largest`, `-fill-holes`)
- More SCT commands (e.g. vertebral labeling, lesion segmentation, CSA)
