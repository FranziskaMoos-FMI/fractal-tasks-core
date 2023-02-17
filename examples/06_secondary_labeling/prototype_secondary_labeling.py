import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Literal
from typing import Optional
from typing import Sequence

import anndata as ad
import dask.array as da
import numpy as np
import zarr
from cellpose import models
from cellpose.core import use_gpu
from devtools import debug

import fractal_tasks_core
from fractal_tasks_core.lib_channels import ChannelNotFoundError
from fractal_tasks_core.lib_channels import get_channel_from_image_zarr
from fractal_tasks_core.lib_pyramid_creation import build_pyramid
from fractal_tasks_core.lib_regions_of_interest import (
    convert_ROI_table_to_indices,
)
from fractal_tasks_core.lib_zattrs_utils import extract_zyx_pixel_sizes
from fractal_tasks_core.lib_zattrs_utils import rescale_datasets

# from anndata.experimental import write_elem

logger = logging.getLogger(__name__)


__OME_NGFF_VERSION__ = fractal_tasks_core.__OME_NGFF_VERSION__


def segment_FOV(
    column: np.ndarray,
    model=None,
    do_3D: bool = True,
    anisotropy=None,
    diameter: float = 40.0,
    cellprob_threshold: float = 0.0,
    flow_threshold: float = 0.4,
    label_dtype=None,
    well_id: str = None,
):
    """
    Internal function that runs Cellpose segmentation for a single ROI.
    """

    # Write some debugging info
    logger.info(
        f"[{well_id}][segment_FOV] START Cellpose |"
        f" column: {type(column)}, {column.shape} |"
        f" do_3D: {do_3D} |"
        f" model.diam_mean: {model.diam_mean} |"
        f" diameter: {diameter} |"
        f" flow threshold: {flow_threshold}"
    )

    # Actual labeling
    t0 = time.perf_counter()
    mask, flows, styles = model.eval(
        column,
        channels=[0, 0],
        do_3D=do_3D,
        net_avg=False,
        augment=False,
        diameter=diameter,
        anisotropy=anisotropy,
        cellprob_threshold=cellprob_threshold,
        flow_threshold=flow_threshold,
    )
    if not do_3D:
        mask = np.expand_dims(mask, axis=0)
    t1 = time.perf_counter()

    # Write some debugging info
    logger.info(
        f"[{well_id}][segment_FOV] END   Cellpose |"
        f" Elapsed: {t1-t0:.4f} seconds |"
        f" mask shape: {mask.shape},"
        f" mask dtype: {mask.dtype} (before recast to {label_dtype}),"
        f" max(mask): {np.max(mask)} |"
        f" model.diam_mean: {model.diam_mean} |"
        f" diameter: {diameter} |"
        f" flow threshold: {flow_threshold}"
    )

    return mask.astype(label_dtype)


def cellpose_segmentation_bis(
    *,
    # Fractal arguments
    input_paths: Sequence[Path],
    output_path: Path,
    component: str,
    metadata: Dict[str, Any],
    # Task-specific arguments
    level: int,
    wavelength_id: Optional[str] = None,
    channel_label: Optional[str] = None,
    relabeling: bool = True,
    anisotropy: Optional[float] = None,
    diameter_level0: float = 80.0,
    cellprob_threshold: float = 0.0,
    flow_threshold: float = 0.4,
    ROI_table_name: str = "FOV_ROI_table",
    bounding_box_ROI_table_name: Optional[str] = None,
    output_label_name: Optional[str] = None,
    model_type: Literal["nuclei", "cyto", "cyto2"] = "nuclei",
    pretrained_model: Optional[str] = None,
    primary_label_ROI_table_name: Optional[str] = None,
) -> Dict[str, Any]:

    # Set input path
    if len(input_paths) > 1:
        raise NotImplementedError
    in_path = input_paths[0].parent
    zarrurl = (in_path.resolve() / component).as_posix() + "/"
    logger.info(zarrurl)

    # Preliminary check
    if (channel_label is None and wavelength_id is None) or (
        channel_label and wavelength_id
    ):
        raise ValueError(
            f"One and only one of {channel_label=} and "
            f"{wavelength_id=} arguments must be provided"
        )

    # Read useful parameters from metadata
    num_levels = metadata["num_levels"]
    coarsening_xy = metadata["coarsening_xy"]

    plate, well = component.split(".zarr/")

    # Find well ID
    well_id = well.replace("/", "_")[:-1]

    # Find channel index
    try:
        channel = get_channel_from_image_zarr(
            image_zarr_path=zarrurl,
            wavelength_id=wavelength_id,
            label=channel_label,
        )
    except ChannelNotFoundError as e:
        logger.warning(
            "Channel not found, exit from the task.\n"
            f"Original error: {str(e)}"
        )
        return {}
    ind_channel = channel["index"]

    # Load ZYX data
    data_zyx = da.from_zarr(f"{zarrurl}{level}")[ind_channel]
    logger.info(f"[{well_id}] {data_zyx.shape=}")

    # Read ROI table
    xxx = f"{zarrurl}tables/{ROI_table_name}"
    logger.warning(f"{xxx=}")
    ROI_table = ad.read_zarr(f"{zarrurl}tables/{ROI_table_name}")

    # Read pixel sizes from zattrs file
    full_res_pxl_sizes_zyx = extract_zyx_pixel_sizes(
        f"{zarrurl}.zattrs", level=0
    )

    actual_res_pxl_sizes_zyx = extract_zyx_pixel_sizes(
        f"{zarrurl}.zattrs", level=level
    )
    print(actual_res_pxl_sizes_zyx)
    # Create list of indices for 3D FOVs spanning the entire Z direction
    print("ROI_table:")
    print(ROI_table)
    print()
    print(f"{full_res_pxl_sizes_zyx=}")
    list_indices = convert_ROI_table_to_indices(
        ROI_table,
        level=level,
        coarsening_xy=coarsening_xy,
        full_res_pxl_sizes_zyx=full_res_pxl_sizes_zyx,
        origin_xyz=(0, 0, 0),
    )

    # Extract image size from FOV-ROI indices
    # Note: this works at level=0, where FOVs should all be of the exact same
    #       size (in pixels)
    """
    FOV_ROI_table = ad.read_zarr(f"{zarrurl}tables/FOV_ROI_table")
    list_FOV_indices_level0 = convert_ROI_table_to_indices(
        FOV_ROI_table,
        level=0,
        full_res_pxl_sizes_zyx=full_res_pxl_sizes_zyx,
    )
    ref_img_size = None
    for indices in list_FOV_indices_level0:
        img_size = (indices[3] - indices[2], indices[5] - indices[4])
        if ref_img_size is None:
            ref_img_size = img_size
        else:
            if img_size != ref_img_size:
                raise Exception(
                    "ERROR: inconsistent image sizes in "
                    f"{list_FOV_indices_level0=}"
                )
    img_size_y, img_size_x = img_size[:]
    """

    # Select 2D/3D behavior and set some parameters
    do_3D = data_zyx.shape[0] > 1
    if do_3D:
        if anisotropy is None:
            # Read pixel sizes from zattrs file
            pxl_zyx = extract_zyx_pixel_sizes(zarrurl + ".zattrs", level=level)
            pixel_size_z, pixel_size_y, pixel_size_x = pxl_zyx[:]
            logger.info(f"[{well_id}] {pxl_zyx=}")
            if not np.allclose(pixel_size_x, pixel_size_y):
                raise Exception(
                    "ERROR: XY anisotropy detected"
                    f"pixel_size_x={pixel_size_x}"
                    f"pixel_size_y={pixel_size_y}"
                )
            anisotropy = pixel_size_z / pixel_size_x

    # Prelminary checks on Cellpose model
    if pretrained_model is None:
        if model_type not in ["nuclei", "cyto2", "cyto"]:
            raise ValueError(f"ERROR model_type={model_type} is not allowed.")
    else:
        if not os.path.exists(pretrained_model):
            raise ValueError(f"{pretrained_model=} does not exist.")

    # Load zattrs file
    zattrs_file = f"{zarrurl}.zattrs"
    with open(zattrs_file, "r") as jsonfile:
        zattrs = json.load(jsonfile)

    # Preliminary checks on multiscales
    multiscales = zattrs["multiscales"]
    if len(multiscales) > 1:
        raise NotImplementedError(
            f"Found {len(multiscales)} multiscales, "
            "but only one is currently supported."
        )
    if "coordinateTransformations" in multiscales[0].keys():
        raise NotImplementedError(
            "global coordinateTransformations at the multiscales "
            "level are not currently supported"
        )

    # Set channel label - FIXME: adapt to new channels structure
    if output_label_name is None:
        try:
            omero_label = zattrs["omero"]["channels"][ind_channel]["label"]
            output_label_name = f"label_{omero_label}"
        except (KeyError, IndexError):
            output_label_name = f"label_{ind_channel}"

    # Rescale datasets (only relevant for level>0)
    new_datasets = rescale_datasets(
        datasets=multiscales[0]["datasets"],
        coarsening_xy=coarsening_xy,
        reference_level=level,
    )

    # Write zattrs for labels and for specific label
    # FIXME deal with: (1) many channels
    # Create labels zarr group and combine existing/new labels in .zattrs
    new_labels = [output_label_name]
    try:
        with open(f"{zarrurl}labels/.zattrs", "r") as f_zattrs:
            existing_labels = json.load(f_zattrs)["labels"]
    except FileNotFoundError:
        existing_labels = []
    intersection = set(new_labels) & set(existing_labels)
    logger.info(f"{new_labels=}")
    logger.info(f"{existing_labels=}")
    if intersection:
        raise RuntimeError(
            f"Labels {intersection} already exist " "but are part of outputs"
        )
    labels_group = zarr.group(f"{zarrurl}labels")
    labels_group.attrs["labels"] = existing_labels + new_labels

    label_group = labels_group.create_group(output_label_name)
    label_group.attrs["image-label"] = {"version": __OME_NGFF_VERSION__}
    label_group.attrs["multiscales"] = [
        {
            "name": output_label_name,
            "version": __OME_NGFF_VERSION__,
            "axes": [
                ax for ax in multiscales[0]["axes"] if ax["type"] != "channel"
            ],
            "datasets": new_datasets,
        }
    ]

    # Open new zarr group for mask 0-th level
    logger.info(f"[{well_id}] {zarrurl}labels/{output_label_name}/0")
    zarr.group(f"{zarrurl}/labels")
    zarr.group(f"{zarrurl}/labels/{output_label_name}")
    store = zarr.storage.FSStore(f"{zarrurl}labels/{output_label_name}/0")
    label_dtype = np.uint32
    mask_zarr = zarr.create(
        shape=data_zyx.shape,
        chunks=data_zyx.chunksize,
        dtype=label_dtype,
        store=store,
        overwrite=False,
        dimension_separator="/",
    )
    print(mask_zarr)

    logger.info(
        f"[{well_id}] "
        f"mask will have shape {data_zyx.shape} "
        f"and chunks {data_zyx.chunks}"
    )

    # Initialize cellpose
    gpu = use_gpu()
    if pretrained_model:
        model = models.CellposeModel(
            gpu=gpu, pretrained_model=pretrained_model
        )
    else:
        model = models.CellposeModel(gpu=gpu, model_type=model_type)
        print(model)

    # Initialize other things
    logger.info(f"[{well_id}] Start cellpose_segmentation task for {zarrurl}")
    logger.info(f"[{well_id}] relabeling: {relabeling}")
    logger.info(f"[{well_id}] do_3D: {do_3D}")
    logger.info(f"[{well_id}] use_gpu: {gpu}")
    logger.info(f"[{well_id}] level: {level}")
    logger.info(f"[{well_id}] model_type: {model_type}")
    logger.info(f"[{well_id}] pretrained_model: {pretrained_model}")
    logger.info(f"[{well_id}] anisotropy: {anisotropy}")
    logger.info(f"[{well_id}] Total well shape/chunks:")
    logger.info(f"[{well_id}] {data_zyx.shape}")
    logger.info(f"[{well_id}] {data_zyx.chunks}")

    # Iterate over ROIs
    num_ROIs = len(list_indices)

    logger.info(f"[{well_id}] Now starting loop over {num_ROIs} ROIs")
    for i_ROI, indices in enumerate(list_indices):

        logger.info(f"[{well_id}] Now processing ROI {i_ROI+1}/{num_ROIs}")

        # Define region
        s_z, e_z, s_y, e_y, s_x, e_x = indices[:]
        print()
        print(f"indices: {indices}")
        print()

        # Prepare input for cellpose
        input_image_array = data_zyx[s_z:e_z, s_y:e_y, s_x:e_x].compute()
        print("input_image_array:")
        print(input_image_array.shape)
        print(input_image_array)
        print()

        # Load current mask
        organoid_labels = da.from_zarr(
            f"{zarrurl}labels/{primary_label_ROI_table_name}/0"
        )[s_z:e_z, s_y:e_y, s_x:e_x].compute()
        print("organoid_labels:")
        print(organoid_labels.shape)
        print(organoid_labels)
        print()

        label_value = int(ROI_table.obs.index[i_ROI]) + 1
        debug(label_value)
        debug(organoid_labels)
        background_mask = organoid_labels != label_value

        # Filter out background from input
        input_image_array[background_mask] = 0

        # FIXME which level should we load here?
        old_mask = da.from_zarr(
            f"{zarrurl}labels/{output_label_name}/0"
        )[  # noqa
            s_z:e_z, s_y:e_y, s_x:e_x
        ].compute()
        new_mask = np.zeros_like(input_image_array)
        print("new_mask:")
        print(new_mask.shape)
        print(new_mask)
        print()

        this_debug = False
        if this_debug:
            # new_mask = np.ones_like(input_image_array)
            size = input_image_array.shape
            new_mask = np.random.choice((label_value, 0), size=size)
        else:
            new_mask = segment_FOV(
                input_image_array,
                model=model,
                do_3D=do_3D,
                anisotropy=anisotropy,
                label_dtype=label_dtype,
                diameter=diameter_level0 / coarsening_xy**level,
                cellprob_threshold=cellprob_threshold,
                flow_threshold=flow_threshold,
                well_id=well_id,
            )

        new_mask[background_mask] = old_mask[background_mask]

        print(new_mask)

        region = (
            slice(s_z, e_z),
            slice(s_y, e_y),
            slice(s_x, e_x),
        )
        # Compute and store 0-th level to disk
        da.array(new_mask).to_zarr(
            url=mask_zarr,
            region=region,
            compute=True,
        )

    logger.info(
        f"[{well_id}] End cellpose_segmentation task for {zarrurl}, "
        "now building pyramids."
    )

    # Starting from on-disk highest-resolution data, build and write to disk a
    # pyramid of coarser levels
    build_pyramid(
        zarrurl=f"{zarrurl}labels/{output_label_name}",
        overwrite=False,
        num_levels=num_levels,
        coarsening_xy=coarsening_xy,
        chunksize=data_zyx.chunksize,
        aggregation_function=np.max,
    )

    logger.info(f"[{well_id}] End building pyramids, exit")

    return {}
