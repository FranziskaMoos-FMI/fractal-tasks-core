# Copyright 2022 (C) Friedrich Miescher Institute for Biomedical Research and
# University of Zurich
#
# Original authors:
# Tommaso Comparin <tommaso.comparin@exact-lab.it>
# Marco Franzon <marco.franzon@exact-lab.it>
#
# This file is part of Fractal and was originally developed by eXact lab S.r.l.
# <exact-lab.it> under contract with Liberali Lab from the Friedrich Miescher
# Institute for Biomedical Research and Pelkmans Lab from the University of
# Zurich.
"""
Wrapper of napari-workflows.
"""
import logging
from pathlib import Path
from typing import Any
from typing import Sequence

import anndata as ad
import dask.array as da
import napari_workflows
import numpy as np
import pandas as pd
import zarr
from napari_workflows._io_yaml_v1 import load_workflow
from pydantic.decorator import validate_arguments

import fractal_tasks_core
from fractal_tasks_core.channels import get_channel_from_image_zarr
from fractal_tasks_core.input_models import NapariWorkflowsInput
from fractal_tasks_core.input_models import NapariWorkflowsOutput
from fractal_tasks_core.lib_ngff import load_NgffImageMeta
from fractal_tasks_core.lib_pyramid_creation import build_pyramid
from fractal_tasks_core.lib_upscale_array import upscale_array
from fractal_tasks_core.lib_write import prepare_label_group
from fractal_tasks_core.lib_zattrs_utils import rescale_datasets
from fractal_tasks_core.roi import check_valid_ROI_indices
from fractal_tasks_core.roi import (
    convert_ROI_table_to_indices,
)
from fractal_tasks_core.roi import load_region
from fractal_tasks_core.tables import write_table


__OME_NGFF_VERSION__ = fractal_tasks_core.__OME_NGFF_VERSION__


logger = logging.getLogger(__name__)


class OutOfTaskScopeError(NotImplementedError):
    """
    Encapsulates features that are out-of-scope for the current wrapper task.
    """

    pass


@validate_arguments
def napari_workflows_wrapper(
    *,
    # Default arguments for fractal tasks:
    input_paths: Sequence[str],
    output_path: str,
    component: str,
    metadata: dict[str, Any],
    # Task-specific arguments:
    workflow_file: str,
    input_specs: dict[str, NapariWorkflowsInput],
    output_specs: dict[str, NapariWorkflowsOutput],
    input_ROI_table: str = "FOV_ROI_table",
    level: int = 0,
    relabeling: bool = True,
    expected_dimensions: int = 3,
    overwrite: bool = True,
):
    """
    Run a napari-workflow on the ROIs of a single OME-NGFF image.

    This task takes images and labels and runs a napari-workflow on them that
    can produce a label and tables as output.

    Examples of allowed entries for `input_specs` and `output_specs`:

    ```
    input_specs = {
        "in_1": {"type": "image", "channel": {"wavelength_id": "A01_C02"}},
        "in_2": {"type": "image", "channel": {"label": "DAPI"}},
        "in_3": {"type": "label", "label_name": "label_DAPI"},
    }

    output_specs = {
        "out_1": {"type": "label", "label_name": "label_DAPI_new"},
        "out_2": {"type": "dataframe", "table_name": "measurements"},
    }
    ```

    Args:
        input_paths: List of input paths where the image data is stored as
            OME-Zarrs. Should point to the parent folder containing one or many
            OME-Zarr files, not the actual OME-Zarr file.
            Example: `["/some/path/"]`.
            his task only supports a single input path.
            (standard argument for Fractal tasks, managed by Fractal server).
        output_path: This parameter is not used by this task.
            (standard argument for Fractal tasks, managed by Fractal server).
        component: Path to the OME-Zarr image in the OME-Zarr plate that is
            processed.
            Example: `"some_plate.zarr/B/03/0"`.
            (standard argument for Fractal tasks, managed by Fractal server).
        metadata: This parameter is not used by this task.
            (standard argument for Fractal tasks, managed by Fractal server).
        workflow_file: Absolute path to napari-workflows YAML file
        input_specs: A dictionary of `NapariWorkflowsInput` values.
        output_specs: A dictionary of `NapariWorkflowsOutput` values.
        input_ROI_table: Name of the ROI table over which the task loops to
            apply napari workflows.
            Examples:
            `FOV_ROI_table`
            => loop over the field of views;
            `organoid_ROI_table`
            => loop over the organoid ROI table (generated by another task);
            `well_ROI_table`
            => process the whole well as one image.
        level: Pyramid level of the image to be used as input for
            napari-workflows. Choose `0` to process at full resolution.
            Levels > 0 are currently only supported for workflows that only
            have intensity images as input and only produce a label images as
            output.
        relabeling: If `True`, apply relabeling so that label values are
            unique across all ROIs in the well.
        expected_dimensions: Expected dimensions (either `2` or `3`). Useful
            when loading 2D images that are stored in a 3D array with shape
            `(1, size_x, size_y)` [which is the default way Fractal stores 2D
            images], but you want to make sure the napari workflow gets a 2D
            array to process. Also useful to set to `2` when loading a 2D
            OME-Zarr that is saved as `(size_x, size_y)`.
        overwrite: If `True`, overwrite the task output.
    """
    wf: napari_workflows.Worfklow = load_workflow(workflow_file)
    logger.info(f"Loaded workflow from {workflow_file}")

    # Validation of input/output specs
    if not (set(wf.leafs()) <= set(output_specs.keys())):
        msg = f"Some item of {wf.leafs()=} is not part of {output_specs=}."
        logger.warning(msg)
    if not (set(wf.roots()) <= set(input_specs.keys())):
        msg = f"Some item of {wf.roots()=} is not part of {input_specs=}."
        logger.error(msg)
        raise ValueError(msg)
    list_outputs = sorted(output_specs.keys())

    # Characterization of workflow and scope restriction
    input_types = [in_params.type for (name, in_params) in input_specs.items()]
    output_types = [
        out_params.type for (name, out_params) in output_specs.items()
    ]
    are_inputs_all_images = set(input_types) == {"image"}
    are_outputs_all_labels = set(output_types) == {"label"}
    are_outputs_all_dataframes = set(output_types) == {"dataframe"}
    is_labeling_workflow = are_inputs_all_images and are_outputs_all_labels
    is_measurement_only_workflow = are_outputs_all_dataframes
    # Level-related constraint
    logger.info(f"This workflow acts at {level=}")
    logger.info(
        f"Is the current workflow a labeling one? {is_labeling_workflow}"
    )
    if level > 0 and not is_labeling_workflow:
        msg = (
            f"{level=}>0 is currently only accepted for labeling workflows, "
            "i.e. those going from image(s) to label(s)"
        )
        logger.error(msg)
        raise OutOfTaskScopeError(msg)
    # Relabeling-related (soft) constraint
    if is_measurement_only_workflow and relabeling:
        logger.warning(
            "This is a measurement-output-only workflow, setting "
            "relabeling=False."
        )
        relabeling = False
    if relabeling:
        max_label_for_relabeling = 0

    # Pre-processing of task inputs
    if len(input_paths) > 1:
        raise NotImplementedError(
            "We currently only support a single input path"
        )
    in_path = Path(input_paths[0]).as_posix()
    label_dtype = np.uint32

    # Read ROI table
    zarrurl = f"{in_path}/{component}"
    ROI_table = ad.read_zarr(f"{in_path}/{component}/tables/{input_ROI_table}")

    # Load image metadata
    ngff_image_meta = load_NgffImageMeta(zarrurl)
    num_levels = ngff_image_meta.num_levels
    coarsening_xy = ngff_image_meta.coarsening_xy

    # Read pixel sizes from zattrs file
    full_res_pxl_sizes_zyx = ngff_image_meta.get_pixel_sizes_zyx(level=0)

    # Create list of indices for 3D FOVs spanning the entire Z direction
    list_indices = convert_ROI_table_to_indices(
        ROI_table,
        level=level,
        coarsening_xy=coarsening_xy,
        full_res_pxl_sizes_zyx=full_res_pxl_sizes_zyx,
    )
    check_valid_ROI_indices(list_indices, input_ROI_table)
    num_ROIs = len(list_indices)
    logger.info(
        f"Completed reading ROI table {input_ROI_table},"
        f" found {num_ROIs} ROIs."
    )

    # Input preparation: "image" type
    image_inputs = [
        (name, in_params)
        for (name, in_params) in input_specs.items()
        if in_params.type == "image"
    ]
    input_image_arrays = {}
    if image_inputs:
        img_array = da.from_zarr(f"{in_path}/{component}/{level}")
        # Loop over image inputs and assign corresponding channel of the image
        for (name, params) in image_inputs:
            channel = get_channel_from_image_zarr(
                image_zarr_path=f"{in_path}/{component}",
                wavelength_id=params.channel.wavelength_id,
                label=params.channel.label,
            )
            channel_index = channel.index
            input_image_arrays[name] = img_array[channel_index]

            # Handle dimensions
            shape = input_image_arrays[name].shape
            if expected_dimensions == 3 and shape[0] == 1:
                logger.warning(
                    f"Input {name} has shape {shape} "
                    f"but {expected_dimensions=}"
                )
            if expected_dimensions == 2:
                if len(shape) == 2:
                    # We already load the data as a 2D array
                    pass
                elif shape[0] == 1:
                    input_image_arrays[name] = input_image_arrays[name][
                        0, :, :
                    ]
                else:
                    msg = (
                        f"Input {name} has shape {shape} "
                        f"but {expected_dimensions=}"
                    )
                    logger.error(msg)
                    raise ValueError(msg)
            logger.info(f"Prepared input with {name=} and {params=}")
        logger.info(f"{input_image_arrays=}")

    # Input preparation: "label" type
    label_inputs = [
        (name, in_params)
        for (name, in_params) in input_specs.items()
        if in_params.type == "label"
    ]
    if label_inputs:
        # Set target_shape for upscaling labels
        if not image_inputs:
            logger.warning(
                f"{len(label_inputs)=} but num_image_inputs=0. "
                "Label array(s) will not be upscaled."
            )
            upscale_labels = False
        else:
            target_shape = list(input_image_arrays.values())[0].shape
            upscale_labels = True
        # Loop over label inputs and load corresponding (upscaled) image
        input_label_arrays = {}
        for (name, params) in label_inputs:
            label_name = params.label_name
            label_array_raw = da.from_zarr(
                f"{in_path}/{component}/labels/{label_name}/{level}"
            )
            input_label_arrays[name] = label_array_raw

            # Handle dimensions
            shape = input_label_arrays[name].shape
            if expected_dimensions == 3 and shape[0] == 1:
                logger.warning(
                    f"Input {name} has shape {shape} "
                    f"but {expected_dimensions=}"
                )
            if expected_dimensions == 2:
                if len(shape) == 2:
                    # We already load the data as a 2D array
                    pass
                elif shape[0] == 1:
                    input_label_arrays[name] = input_label_arrays[name][
                        0, :, :
                    ]
                else:
                    msg = (
                        f"Input {name} has shape {shape} "
                        f"but {expected_dimensions=}"
                    )
                    logger.error(msg)
                    raise ValueError(msg)

            if upscale_labels:
                # Check that dimensionality matches the image
                if len(input_label_arrays[name].shape) != len(target_shape):
                    raise ValueError(
                        f"Label {name} has shape "
                        f"{input_label_arrays[name].shape}. "
                        "But the corresponding image has shape "
                        f"{target_shape}. Those dimensionalities do not "
                        f"match. Is {expected_dimensions=} the correct "
                        "setting?"
                    )
                if expected_dimensions == 3:
                    upscaling_axes = [1, 2]
                else:
                    upscaling_axes = [0, 1]
                input_label_arrays[name] = upscale_array(
                    array=input_label_arrays[name],
                    target_shape=target_shape,
                    axis=upscaling_axes,
                    pad_with_zeros=True,
                )

            logger.info(f"Prepared input with {name=} and {params=}")
        logger.info(f"{input_label_arrays=}")

    # Output preparation: "label" type
    label_outputs = [
        (name, out_params)
        for (name, out_params) in output_specs.items()
        if out_params.type == "label"
    ]
    if label_outputs:
        # Preliminary scope checks
        if len(label_outputs) > 1:
            raise OutOfTaskScopeError(
                "Multiple label outputs would break label-inputs-only "
                f"workflows (found {len(label_outputs)=})."
            )
        if len(label_outputs) > 1 and relabeling:
            raise OutOfTaskScopeError(
                "Multiple label outputs would break relabeling in labeling+"
                f"measurement workflows (found {len(label_outputs)=})."
            )

        # We only support two cases:
        # 1. If there exist some input images, then use the first one to
        #    determine output-label array properties
        # 2. If there are no input images, but there are input labels, then (A)
        #    re-load the pixel sizes and re-build ROI indices, and (B) use the
        #    first input label to determine output-label array properties
        if image_inputs:
            reference_array = list(input_image_arrays.values())[0]
        elif label_inputs:
            reference_array = list(input_label_arrays.values())[0]
            # Re-load pixel size, matching to the correct level
            input_label_name = label_inputs[0][1].label_name
            ngff_label_image_meta = load_NgffImageMeta(
                f"{in_path}/{component}/labels/{input_label_name}"
            )
            full_res_pxl_sizes_zyx = ngff_label_image_meta.get_pixel_sizes_zyx(
                level=0
            )
            # Create list of indices for 3D FOVs spanning the whole Z direction
            list_indices = convert_ROI_table_to_indices(
                ROI_table,
                level=level,
                coarsening_xy=coarsening_xy,
                full_res_pxl_sizes_zyx=full_res_pxl_sizes_zyx,
            )
            check_valid_ROI_indices(list_indices, input_ROI_table)
            num_ROIs = len(list_indices)
            logger.info(
                f"Re-create ROI indices from ROI table {input_ROI_table}, "
                f"using {full_res_pxl_sizes_zyx=}. "
                "This is necessary because label-input-only workflows may "
                "have label inputs that are at a different resolution and "
                "are not upscaled."
            )
        else:
            msg = (
                "Missing image_inputs and label_inputs, we cannot assign"
                " label output properties"
            )
            raise OutOfTaskScopeError(msg)

        # Extract label properties from reference_array, and make sure they are
        # for three dimensions
        label_shape = reference_array.shape
        label_chunksize = reference_array.chunksize
        if len(label_shape) == 2 and len(label_chunksize) == 2:
            if expected_dimensions == 3:
                raise ValueError(
                    f"Something wrong: {label_shape=} but "
                    f"{expected_dimensions=}"
                )
            label_shape = (1, label_shape[0], label_shape[1])
            label_chunksize = (1, label_chunksize[0], label_chunksize[1])
        logger.info(f"{label_shape=}")
        logger.info(f"{label_chunksize=}")

        # Loop over label outputs and (1) set zattrs, (2) create zarr group
        output_label_zarr_groups: dict[str, Any] = {}
        for (name, out_params) in label_outputs:

            # (1a) Rescale OME-NGFF datasets (relevant for level>0)
            if not ngff_image_meta.multiscale.axes[0].name == "c":
                raise ValueError(
                    "Cannot set `remove_channel_axis=True` for multiscale "
                    f"metadata with axes={ngff_image_meta.multiscale.axes}. "
                    'First axis should have name "c".'
                )
            new_datasets = rescale_datasets(
                datasets=[
                    ds.dict() for ds in ngff_image_meta.multiscale.datasets
                ],
                coarsening_xy=coarsening_xy,
                reference_level=level,
                remove_channel_axis=True,
            )

            # (1b) Prepare attrs for label group
            label_name = out_params.label_name
            label_attrs = {
                "image-label": {
                    "version": __OME_NGFF_VERSION__,
                    "source": {"image": "../../"},
                },
                "multiscales": [
                    {
                        "name": label_name,
                        "version": __OME_NGFF_VERSION__,
                        "axes": [
                            ax.dict()
                            for ax in ngff_image_meta.multiscale.axes
                            if ax.type != "channel"
                        ],
                        "datasets": new_datasets,
                    }
                ],
            }

            # (2) Prepare label group
            zarrurl = f"{in_path}/{component}"
            image_group = zarr.group(zarrurl)
            label_group = prepare_label_group(
                image_group,
                label_name,
                overwrite=overwrite,
                label_attrs=label_attrs,
                logger=logger,
            )
            logger.info(
                "Helper function `prepare_label_group` returned "
                f"{label_group=}"
            )

            # (3) Create zarr group at level=0
            store = zarr.storage.FSStore(
                f"{in_path}/{component}/labels/{label_name}/0"
            )
            mask_zarr = zarr.create(
                shape=label_shape,
                chunks=label_chunksize,
                dtype=label_dtype,
                store=store,
                overwrite=overwrite,
                dimension_separator="/",
            )
            output_label_zarr_groups[name] = mask_zarr
            logger.info(f"Prepared output with {name=} and {out_params=}")
        logger.info(f"{output_label_zarr_groups=}")

    # Output preparation: "dataframe" type
    dataframe_outputs = [
        (name, out_params)
        for (name, out_params) in output_specs.items()
        if out_params.type == "dataframe"
    ]
    output_dataframe_lists: dict[str, list] = {}
    for (name, out_params) in dataframe_outputs:
        output_dataframe_lists[name] = []
        logger.info(f"Prepared output with {name=} and {out_params=}")
        logger.info(f"{output_dataframe_lists=}")

    #####

    for i_ROI, indices in enumerate(list_indices):
        s_z, e_z, s_y, e_y, s_x, e_x = indices[:]
        region = (slice(s_z, e_z), slice(s_y, e_y), slice(s_x, e_x))

        logger.info(f"ROI {i_ROI+1}/{num_ROIs}: {region=}")

        # Always re-load napari worfklow
        wf = load_workflow(workflow_file)

        # Set inputs
        for input_name in input_specs.keys():
            input_type = input_specs[input_name].type

            if input_type == "image":
                wf.set(
                    input_name,
                    load_region(
                        input_image_arrays[input_name],
                        region,
                        compute=True,
                        return_as_3D=False,
                    ),
                )
            elif input_type == "label":
                wf.set(
                    input_name,
                    load_region(
                        input_label_arrays[input_name],
                        region,
                        compute=True,
                        return_as_3D=False,
                    ),
                )

        # Get outputs
        outputs = wf.get(list_outputs)

        # Iterate first over dataframe outputs (to use the correct
        # max_label_for_relabeling, if needed)
        for ind_output, output_name in enumerate(list_outputs):
            if output_specs[output_name].type != "dataframe":
                continue
            df = outputs[ind_output]
            if relabeling:
                df["label"] += max_label_for_relabeling
                logger.info(
                    f'ROI {i_ROI+1}/{num_ROIs}: Relabeling "{name}" dataframe'
                    "output, with {max_label_for_relabeling=}"
                )

            # Append the new-ROI dataframe to the all-ROIs list
            output_dataframe_lists[output_name].append(df)

        # After all dataframe outputs, iterate over label outputs (which
        # actually can be only 0 or 1)
        for ind_output, output_name in enumerate(list_outputs):
            if output_specs[output_name].type != "label":
                continue
            mask = outputs[ind_output]

            # Check dimensions
            if len(mask.shape) != expected_dimensions:
                msg = (
                    f"Output {output_name} has shape {mask.shape} "
                    f"but {expected_dimensions=}"
                )
                logger.error(msg)
                raise ValueError(msg)
            elif expected_dimensions == 2:
                mask = np.expand_dims(mask, axis=0)

            # Sanity check: issue warning for non-consecutive labels
            unique_labels = np.unique(mask)
            num_unique_labels_in_this_ROI = len(unique_labels)
            if np.min(unique_labels) == 0:
                num_unique_labels_in_this_ROI -= 1
            num_labels_in_this_ROI = int(np.max(mask))
            if num_labels_in_this_ROI != num_unique_labels_in_this_ROI:
                logger.warning(
                    f'ROI {i_ROI+1}/{num_ROIs}: "{name}" label output has'
                    f"non-consecutive labels: {num_labels_in_this_ROI=} but"
                    f"{num_unique_labels_in_this_ROI=}"
                )

            if relabeling:
                mask[mask > 0] += max_label_for_relabeling
                logger.info(
                    f'ROI {i_ROI+1}/{num_ROIs}: Relabeling "{name}" label '
                    f"output, with {max_label_for_relabeling=}"
                )
                max_label_for_relabeling += num_labels_in_this_ROI
                logger.info(
                    f"ROI {i_ROI+1}/{num_ROIs}: label-number update with "
                    f"{num_labels_in_this_ROI=}; "
                    f"new {max_label_for_relabeling=}"
                )

            da.array(mask).to_zarr(
                url=output_label_zarr_groups[output_name],
                region=region,
                compute=True,
                overwrite=overwrite,
            )
        logger.info(f"ROI {i_ROI+1}/{num_ROIs}: output handling complete")

    # Output handling: "dataframe" type (for each output, concatenate ROI
    # dataframes, clean up, and store in a AnnData table on-disk)
    for (name, out_params) in dataframe_outputs:
        table_name = out_params.table_name
        # Concatenate all FOV dataframes
        list_dfs = output_dataframe_lists[name]
        if len(list_dfs) == 0:
            measurement_table = ad.AnnData()
        else:
            df_well = pd.concat(list_dfs, axis=0, ignore_index=True)
            # Extract labels and drop them from df_well
            labels = pd.DataFrame(df_well["label"].astype(str))
            df_well.drop(labels=["label"], axis=1, inplace=True)
            # Convert all to float (warning: some would be int, in principle)
            measurement_dtype = np.float32
            df_well = df_well.astype(measurement_dtype)
            df_well.index = df_well.index.map(str)
            # Convert to anndata
            measurement_table = ad.AnnData(df_well, dtype=measurement_dtype)
            measurement_table.obs = labels

        # Write to zarr group
        image_group = zarr.group(f"{in_path}/{component}")
        table_attrs = dict(
            type="feature_table",
            region=dict(path=f"../labels/{out_params.label_name}"),
            instance_key="label",
        )
        write_table(
            image_group,
            table_name,
            measurement_table,
            overwrite=overwrite,
            table_attrs=table_attrs,
        )

    # Output handling: "label" type (for each output, build and write to disk
    # pyramid of coarser levels)
    for (name, out_params) in label_outputs:
        label_name = out_params.label_name
        build_pyramid(
            zarrurl=f"{zarrurl}/labels/{label_name}",
            overwrite=overwrite,
            num_levels=num_levels,
            coarsening_xy=coarsening_xy,
            chunksize=label_chunksize,
            aggregation_function=np.max,
        )

    return {}


if __name__ == "__main__":
    from fractal_tasks_core.tasks._utils import run_fractal_task

    run_fractal_task(
        task_function=napari_workflows_wrapper,
        logger_name=logger.name,
    )
