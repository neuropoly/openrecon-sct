"""
OpenRecon module running Spinal Cord Toolbox (SCT) commands on incoming images.

Pipeline (per image series):
    MRD images -> NIfTI volume -> SCT command (subprocess) -> NIfTI result -> MRD images

Currently supported SCT commands:
    - DeepsegSpinalcord : `sct_deepseg spinalcord` (contrast-agnostic spinal cord segmentation)

Output series (each one gets its own series index):
    - original images                       (if SaveOriginalImages)
    - segmentation mask        _SCTSeg
    - original + seg contour   _SCTOverlay  (if SaveOverlay)
"""

import ismrmrd
import os
import shutil
import subprocess
import tempfile
import logging
import traceback
import xml.dom.minidom
import base64
import ctypes
import numpy as np
import nibabel as nib
import constants


# SCT task name (as in `sct_deepseg <task>`) for each choice of the `SCTCommand` UI parameter
SCT_COMMANDS = {
    'DeepsegSpinalcord': 'spinalcord',
}


def process(connection, config, metadata):
    logging.info("Config: \n%s", config)

    # Metadata should be MRD formatted header, but may be a string
    # if it failed conversion earlier
    try:
        logging.info("Incoming dataset contains %d encodings", len(metadata.encoding))
        logging.info("First encoding is of type '%s', with a matrix size of (%s x %s x %s) and a field of view of (%s x %s x %s)mm^3",
            metadata.encoding[0].trajectory,
            metadata.encoding[0].encodedSpace.matrixSize.x,
            metadata.encoding[0].encodedSpace.matrixSize.y,
            metadata.encoding[0].encodedSpace.matrixSize.z,
            metadata.encoding[0].encodedSpace.fieldOfView_mm.x,
            metadata.encoding[0].encodedSpace.fieldOfView_mm.y,
            metadata.encoding[0].encodedSpace.fieldOfView_mm.z)

    except:
        logging.info("Improperly formatted metadata: \n%s", metadata)

    # Continuously parse incoming data parsed from MRD messages
    currentSeries = 0
    imgGroup = []
    try:
        for item in connection:
            # ----------------------------------------------------------
            # Raw k-space data messages
            # ----------------------------------------------------------
            if isinstance(item, ismrmrd.Acquisition):
                raise Exception("Raw k-space data is not supported by this module")

            # ----------------------------------------------------------
            # Image data messages
            # ----------------------------------------------------------
            elif isinstance(item, ismrmrd.Image):
                # When the series number changes, process the accumulated group
                if item.image_series_index != currentSeries:
                    logging.info("Processing a group of images because series index changed to %d", item.image_series_index)
                    currentSeries = item.image_series_index
                    image = process_image(imgGroup, connection, config, metadata)
                    connection.send_image(image)
                    imgGroup = []

                # Only process magnitude images -- send phase images back without modification (fallback for images with unknown type)
                if (item.image_type is ismrmrd.IMTYPE_MAGNITUDE) or (item.image_type == 0):
                    imgGroup.append(item)
                else:
                    tmpMeta = ismrmrd.Meta.deserialize(item.attribute_string)
                    tmpMeta['Keep_image_geometry'] = 1
                    item.attribute_string = tmpMeta.serialize()

                    connection.send_image(item)
                    continue

            elif item is None:
                break

            else:
                raise Exception("Unsupported data type %s", type(item).__name__)

        # Process any remaining groups of image data
        if len(imgGroup) > 0:
            logging.info("Processing a group of images (untriggered)")
            image = process_image(imgGroup, connection, config, metadata)
            connection.send_image(image)
            imgGroup = []

    except Exception as e:
        logging.error(traceback.format_exc())
        connection.send_logging(constants.MRD_LOGGING_ERROR, traceback.format_exc())

        # Close connection without sending MRD_MESSAGE_CLOSE message to signal failure
        connection.shutdown_close()

    finally:
        try:
            connection.send_close()
        except:
            logging.error("Failed to send close message!")


def check_OR_arguments(config, arg_name: str, arg_type: type, arg_default):
    """Fetch a UI parameter from the config. In OpenRecon, values arrive as strings, so cast them."""
    arg_value = arg_default

    if isinstance(config, dict) and ('parameters' in config) and (arg_name in config['parameters']):
        arg_value = config['parameters'][arg_name]
        logging.info(f"found config['parameters']['{arg_name}'] : type={type(arg_value)} content={arg_value}")
    else:
        logging.warning(f"config['parameters']['{arg_name}'] NOT FOUND, using default : {arg_default}")

    if arg_type is bool and not isinstance(arg_value, bool):
        if   str(arg_value).lower() == 'true' : arg_value = True
        elif str(arg_value).lower() == 'false': arg_value = False
        else: raise ValueError(f"{arg_name}={arg_value!r} cannot be cast to `bool`")
    elif arg_type in (int, float, str):
        arg_value = arg_type(arg_value)

    logging.info(f'{arg_name} = {arg_value}')
    return arg_value


def mrd_to_nifti(images) -> tuple[nib.Nifti1Image, np.ndarray]:
    """
    Stack 2D MRD images into a 3D NIfTI volume with a proper RAS affine.

    Returns the NIfTI image (array order [x y slice], slices sorted along the slice direction)
    and `order`, the permutation such that volume slice k == images[order[k]].

    Note: only orientation and voxel size matter for the segmentation, but we still
    follow the MRD convention where `position` is the center of the slice.
    """
    head = images[0]
    nx, ny = int(head.matrix_size[0]), int(head.matrix_size[1])  # read (columns), phase (rows)
    fov    = np.array(head.field_of_view[:], dtype=float)
    dx, dy = fov[0] / nx, fov[1] / ny
    read_dir  = np.array(head.read_dir , dtype=float)
    phase_dir = np.array(head.phase_dir, dtype=float)
    slice_dir = np.array(head.slice_dir, dtype=float)

    # Sort slices along the slice direction (images may arrive interleaved)
    positions = np.array([img.position[:] for img in images], dtype=float)
    order     = np.argsort(positions @ slice_dir, kind='stable')
    positions = positions[order]

    if len(images) > 1:
        slice_step = (positions[-1] - positions[0]) / (len(images) - 1)
        steps = np.diff(positions, axis=0)
        if not np.allclose(steps, slice_step, atol=1e-2):
            logging.warning('Slice positions are not equally spaced: the NIfTI geometry is approximate')
    else:
        slice_step = slice_dir * fov[2]  # single slice : use slice thickness

    # Voxel array : MRD img.data is [cha z y x] -> take [y x] -> transpose to [x y]
    data = np.stack([images[i].data[0, 0].T for i in order], axis=-1).astype(np.float32)  # [x y slice]

    # Affine in LPS (DICOM/MRD patient coordinates), then convert to RAS (NIfTI)
    corner = positions[0] - read_dir * (fov[0] - dx) / 2 - phase_dir * (fov[1] - dy) / 2
    affine_lps = np.eye(4)
    affine_lps[:3, 0] = read_dir  * dx
    affine_lps[:3, 1] = phase_dir * dy
    affine_lps[:3, 2] = slice_step
    affine_lps[:3, 3] = corner
    affine_ras = np.diag([-1, -1, 1, 1]) @ affine_lps

    nii = nib.Nifti1Image(data, affine_ras)
    nii.set_qform(affine_ras, code=1)
    nii.set_sform(affine_ras, code=1)
    return nii, order


def nifti_to_ref_orientation(nii: nib.Nifti1Image, ref: nib.Nifti1Image) -> np.ndarray:
    """Return the data of `nii` in the voxel orientation of `ref` (SCT normally preserves it, this is a safeguard)."""
    data = np.asanyarray(nii.dataobj)
    ornt_in  = nib.orientations.io_orientation(nii.affine)
    ornt_ref = nib.orientations.io_orientation(ref.affine)
    if not np.array_equal(ornt_in, ornt_ref):
        logging.warning('SCT output orientation differs from input, reorienting')
        data = nib.orientations.apply_orientation(data, nib.orientations.ornt_transform(ornt_in, ornt_ref))
    if data.shape != ref.shape:
        raise RuntimeError(f'SCT output shape {data.shape} does not match input shape {ref.shape}')
    return data


def find_sct_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None and 'SCT_DIR' in os.environ:
        candidate = os.path.join(os.environ['SCT_DIR'], 'bin', name)
        if os.path.exists(candidate):
            path = candidate
    if path is None:
        raise FileNotFoundError(f'`{name}` not found: is SCT installed and in the PATH (or SCT_DIR set) ?')
    return path


def run_sct_deepseg(nii_in: nib.Nifti1Image, task: str) -> np.ndarray:
    """Run `sct_deepseg <task>` on a NIfTI image and return the segmentation in the input voxel grid."""
    with tempfile.TemporaryDirectory(prefix='or-sct_') as tmpdir:
        fname_in  = os.path.join(tmpdir, 'input.nii.gz')
        dir_out   = os.path.join(tmpdir, 'out')
        os.makedirs(dir_out)
        nib.save(nii_in, fname_in)

        cmd = [find_sct_executable('sct_deepseg'), task, '-i', fname_in, '-o', os.path.join(dir_out, 'input_seg.nii.gz')]
        logging.info(f"Running : {' '.join(cmd)}")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in result.stdout.splitlines():
            logging.info(f'[SCT] {line}')
        if result.returncode != 0:
            raise RuntimeError(f'sct_deepseg failed with exit code {result.returncode}')

        # SCT may append model-specific suffixes to the output name, so look for what was written
        # (e.g. `spinalcord` also writes a `*_cropbox.nii.gz` that we must ignore)
        outputs = sorted(f for f in os.listdir(dir_out) if f.endswith('.nii.gz'))
        logging.info(f'SCT outputs : {outputs}')
        candidates = [f for f in outputs if 'cropbox' not in f]
        if not candidates:
            raise RuntimeError('sct_deepseg did not produce any segmentation output')
        fname_seg = 'input_seg.nii.gz' if 'input_seg.nii.gz' in candidates else candidates[0]
        nii_seg = nib.load(os.path.join(dir_out, fname_seg))
        return nifti_to_ref_orientation(nii_seg, nii_in).astype(np.float32)


def mask_contour(mask: np.ndarray) -> np.ndarray:
    """In-plane (2D, per slice) contour of a binary mask [x y slice], using 4-neighbour erosion."""
    m = mask.astype(bool)
    eroded = m.copy()
    eroded[1:, :]  &= m[:-1, :]
    eroded[:-1, :] &= m[1:, :]
    eroded[:, 1:]  &= m[:, :-1]
    eroded[:, :-1] &= m[:, 1:]
    eroded[0, :] = eroded[-1, :] = eroded[:, 0] = eroded[:, -1] = False
    return m & ~eroded


def create_series(volume_xys: np.ndarray, images, series_offset: int, history: list[str], seq_descrip_add: str) -> list[ismrmrd.Image]:
    """
    Convert a volume [x y image] (in the ORIGINAL order of `images`) into a new MRD series,
    copying headers & meta from the original images.
    """
    images_out = []
    for i, img in enumerate(images):
        # MRD wants [y x] (from_array with transpose=False accepts [y x])
        out = ismrmrd.Image.from_array(volume_xys[:, :, i].T.astype(np.int16), transpose=False)

        # Copy the original header (do not modify the input header in place)
        head = img.getHead()
        head = type(head).from_buffer_copy(head)
        head.data_type = out.data_type
        head.image_series_index += series_offset
        out.setHead(head)

        meta = ismrmrd.Meta.deserialize(img.attribute_string)
        meta['DataRole'] = 'Image'
        if history:
            meta['ImageProcessingHistory'] = history
        if seq_descrip_add:
            meta['SequenceDescriptionAdditional'] = seq_descrip_add
        meta['Keep_image_geometry'] = 1
        out.attribute_string = meta.serialize()

        images_out.append(out)

    logging.debug("Image MetaAttributes[0]: %s", xml.dom.minidom.parseString(images_out[0].attribute_string).toprettyxml())
    logging.info(f'Created series : offset={series_offset} history={history} SequenceDescriptionAdditional={seq_descrip_add!r} n={len(images_out)}')
    return images_out


def process_image(images, connection, config, metadata):

    if len(images) == 0:
        return []

    logging.debug("Processing data with %d images of type %s", len(images), images[0].data.dtype)

    # OR parameters
    SCTCommand         = check_OR_arguments(config, 'SCTCommand'        , str , 'DeepsegSpinalcord')
    SaveOriginalImages = check_OR_arguments(config, 'SaveOriginalImages', bool, True)
    SaveOverlay        = check_OR_arguments(config, 'SaveOverlay'       , bool, True)

    if SCTCommand not in SCT_COMMANDS:
        raise ValueError(f'Unknown SCTCommand {SCTCommand!r}. Supported : {list(SCT_COMMANDS)}')

    meta0 = ismrmrd.Meta.deserialize(images[0].attribute_string)
    logging.info(f"MRD SequenceDescription : {meta0.get('SequenceDescription', '')}")
    if 'IceMiniHead' in meta0:
        logging.debug("IceMiniHead[0]: %s", base64.b64decode(meta0['IceMiniHead']).decode('utf-8'))

    # MRD -> NIfTI (sorted along slice direction)
    nii, order = mrd_to_nifti(images)
    logging.info(f'NIfTI shape : {nii.shape}  zooms : {nii.header.get_zooms()}')
    logging.info(f'NIfTI affine (RAS) :\n{nii.affine}')
    logging.info(f'NIfTI orientation : {"".join(nib.orientations.aff2axcodes(nii.affine))}')

    # Run SCT
    seg_sorted = run_sct_deepseg(nii, SCT_COMMANDS[SCTCommand]) > 0.5
    logging.info(f'Segmentation : {int(seg_sorted.sum())} voxels')

    # Back to the original image order
    inverse = np.argsort(order)
    data_orig = np.asanyarray(nii.dataobj)[:, :, inverse]
    seg       = seg_sorted[:, :, inverse]

    # Display value for the mask/contour : use the image max so it shows bright with the inherited windowing
    bright = max(int(np.percentile(data_orig, 99.9)), 1)

    history_base = f'SCT_{SCTCommand}'
    images_out = []
    series_offset = 0

    if SaveOriginalImages:
        images_out += create_series(data_orig, images, series_offset, [], '')
        series_offset += 1

    images_out += create_series(seg * bright, images, series_offset, [history_base], 'SCTSeg')
    series_offset += 1

    if SaveOverlay:
        overlay = data_orig.copy()
        overlay[mask_contour(seg)] = bright
        images_out += create_series(overlay, images, series_offset, [history_base, 'Overlay'], 'SCTOverlay')
        series_offset += 1

    return images_out
