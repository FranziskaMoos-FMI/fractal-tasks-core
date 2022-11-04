"""
Copyright 2022 (C) Friedrich Miescher Institute for Biomedical Research and
University of Zurich

Original authors:
Marco Franzon <marco.franzon@exact-lab.it>
Tommaso Comparin <tommaso.comparin@exact-lab.it>

This file is part of Fractal and was originally developed by eXact lab S.r.l.
<exact-lab.it> under contract with Liberali Lab from the Friedrich Miescher
Institute for Biomedical Research and Pelkmans Lab from the University of
Zurich.
"""
import glob
import json
import urllib
from pathlib import Path
from typing import Dict

import anndata as ad
import dask.array as da
from devtools import debug
from jsonschema import validate

from fractal_tasks_core import __OME_NGFF_VERSION__
from fractal_tasks_core.create_zarr_structure import create_zarr_structure
from fractal_tasks_core.napari_workflows_wrapper import (
    napari_workflows_wrapper,
)
from fractal_tasks_core.yokogawa_to_zarr import yokogawa_to_zarr


def validate_schema(*, path: str, type: str):
    url: str = (
        "https://raw.githubusercontent.com/ome/ngff/main/"
        f"{__OME_NGFF_VERSION__}/schemas/{type}.schema"
    )
    debug(url)
    with urllib.request.urlopen(url) as fin:
        schema: Dict = json.load(fin)
    debug(path)
    debug(type)
    with open(f"{path}/.zattrs", "r") as fin:
        zattrs = json.load(fin)
    validate(instance=zattrs, schema=schema)


def check_file_number(*, zarr_path: Path):
    """
    Example input:
        zarr_path = Path("/SOME/PATH/plate.zarr/row/col/fov/")

    Relevant glob for zarr_path
        zarr_path / 0 / c / z / y / x

    """
    chunkfiles_on_disk = glob.glob(str(zarr_path / "0/*/*/*/*"))
    debug(chunkfiles_on_disk)
    num_chunkfiles_on_disk = len(chunkfiles_on_disk)

    zarr_chunks = da.from_zarr(str(zarr_path / "0/")).chunks
    debug(zarr_chunks)
    num_chunkfiles_from_zarr = 1
    for c in zarr_chunks:
        num_chunkfiles_from_zarr *= len(c)

    assert num_chunkfiles_from_zarr == num_chunkfiles_on_disk


def validate_labels_and_measurements(
    image_zarr: Path, *, label_name: str, table_name: str
):

    # FIXME: clean up this test and make asserts as strict as possible
    # FIXME: move this test at the end of a napari-workflow task

    label_path = str(image_zarr / "labels" / label_name / "0")
    table_path = str(image_zarr / "tables" / table_name)
    labels = da.from_zarr(label_path)
    list_label_values = list(da.unique(labels).compute())
    assert list_label_values[0] == 0
    list_label_values = list_label_values[1:]

    table = ad.read_zarr(table_path)
    list_table_label_values = [int(x) for x in list(table.obs["label"])]

    # Check that labels are unique in measurement dataframe
    assert len(set(list_table_label_values)) == len(list_table_label_values)

    # Check that labels are the same in measurement dataframe and labels array
    assert list_table_label_values == list_label_values


channel_parameters = {
    "A01_C01": {
        "label": "DAPI",
        "colormap": "00FFFF",
        "start": 0,
        "end": 700,
    },
    "A01_C02": {
        "label": "nanog",
        "colormap": "FF00FF",
        "start": 0,
        "end": 180,
    },
    "A02_C03": {
        "label": "Lamin B1",
        "colormap": "FFFF00",
        "start": 0,
        "end": 1500,
    },
}

num_levels = 6
coarsening_xy = 2


def test_workflow_napari_worfklow(
    tmp_path: Path,
    dataset_10_5281_zenodo_7059515: Path,
    testdata_path: Path,
):

    # Init
    img_path = dataset_10_5281_zenodo_7059515 / "*.png"
    zarr_path = tmp_path / "tmp_out/*.zarr"
    metadata = {}
    debug(zarr_path)

    # Create zarr structure
    metadata_update = create_zarr_structure(
        input_paths=[img_path],
        output_path=zarr_path,
        channel_parameters=channel_parameters,
        num_levels=num_levels,
        coarsening_xy=coarsening_xy,
        metadata_table="mrf_mlf",
    )
    metadata.update(metadata_update)
    debug(metadata)

    # Yokogawa to zarr
    for component in metadata["well"]:
        yokogawa_to_zarr(
            input_paths=[zarr_path],
            output_path=zarr_path,
            metadata=metadata,
            component=component,
        )
    debug(metadata)

    # First napari-workflows task (labeling)
    workflow_file = str(testdata_path / "napari_workflows/wf_1.yaml")
    input_specs = {
        "input": {"type": "image", "channel": "A01_C01"},
    }
    output_specs = {
        "Result of Expand labels (scikit-image, nsbatwm)": {
            "type": "label",
            "label_name": "label_DAPI",
        },
    }
    for component in metadata["well"]:
        napari_workflows_wrapper(
            input_paths=[zarr_path],
            output_path=zarr_path,
            metadata=metadata,
            component=component,
            input_specs=input_specs,
            output_specs=output_specs,
            workflow_file=workflow_file,
            ROI_table_name="FOV_ROI_table",
            level=2,
        )
    debug(metadata)

    # Second napari-workflows task (measurement)
    workflow_file = str(testdata_path / "napari_workflows/wf_4.yaml")
    input_specs = {
        "dapi_img": {"type": "image", "channel": "A01_C01"},
        "dapi_label_img": {"type": "label", "label_name": "label_DAPI"},
    }
    output_specs = {
        "regionprops_DAPI": {
            "type": "dataframe",
            "table_name": "regionprops_DAPI",
        },
    }
    for component in metadata["well"]:
        napari_workflows_wrapper(
            input_paths=[zarr_path],
            output_path=zarr_path,
            metadata=metadata,
            component=component,
            input_specs=input_specs,
            output_specs=output_specs,
            workflow_file=workflow_file,
            ROI_table_name="FOV_ROI_table",
        )
    debug(metadata)

    # OME-NGFF JSON validation
    image_zarr = Path(zarr_path.parent / metadata["well"][0])
    well_zarr = image_zarr.parent
    plate_zarr = image_zarr.parents[2]
    label_zarr = image_zarr / "labels/label_DAPI"
    validate_schema(path=str(image_zarr), type="image")
    validate_schema(path=str(well_zarr), type="well")
    validate_schema(path=str(plate_zarr), type="plate")
    validate_schema(path=str(label_zarr), type="label")

    check_file_number(zarr_path=image_zarr)

    validate_labels_and_measurements(
        image_zarr, label_name="label_DAPI", table_name="regionprops_DAPI"
    )


def test_workflow_napari_worfklow_label_input_only(
    tmp_path: Path,
    dataset_10_5281_zenodo_7059515: Path,
    testdata_path: Path,
):

    # Init
    img_path = dataset_10_5281_zenodo_7059515 / "*.png"
    zarr_path = tmp_path / "tmp_out/*.zarr"
    metadata = {}
    debug(zarr_path)

    # Create zarr structure
    metadata_update = create_zarr_structure(
        input_paths=[img_path],
        output_path=zarr_path,
        channel_parameters=channel_parameters,
        num_levels=num_levels,
        coarsening_xy=coarsening_xy,
        metadata_table="mrf_mlf",
    )
    metadata.update(metadata_update)
    debug(metadata)

    # Yokogawa to zarr
    for component in metadata["well"]:
        yokogawa_to_zarr(
            input_paths=[zarr_path],
            output_path=zarr_path,
            metadata=metadata,
            component=component,
        )
    debug(metadata)

    # First napari-workflows task (labeling)
    workflow_file = str(testdata_path / "napari_workflows/wf_1.yaml")
    input_specs = {
        "input": {"type": "image", "channel": "A01_C01"},
    }
    output_specs = {
        "Result of Expand labels (scikit-image, nsbatwm)": {
            "type": "label",
            "label_name": "label_DAPI",
        },
    }
    for component in metadata["well"]:
        napari_workflows_wrapper(
            input_paths=[zarr_path],
            output_path=zarr_path,
            metadata=metadata,
            component=component,
            input_specs=input_specs,
            output_specs=output_specs,
            workflow_file=workflow_file,
            ROI_table_name="FOV_ROI_table",
            level=2,
        )
    debug(metadata)

    # Second napari-workflows task (measurement)
    workflow_file = str(
        testdata_path / "napari_workflows" / "wf_from_labels_to_labels.yaml"
    )
    input_specs = {
        "test_labels": {"type": "label", "label_name": "label_DAPI"},
    }
    output_specs = {
        "Result of Expand labels (scikit-image, nsbatwm)": {
            "type": "label",
            "label_name": "label_DAPI_expanded",
        },
    }
    for component in metadata["well"]:
        napari_workflows_wrapper(
            input_paths=[zarr_path],
            output_path=zarr_path,
            metadata=metadata,
            component=component,
            input_specs=input_specs,
            output_specs=output_specs,
            workflow_file=workflow_file,
            ROI_table_name="FOV_ROI_table",
        )
    debug(metadata)

    # OME-NGFF JSON validation
    image_zarr = Path(zarr_path.parent / metadata["well"][0])
    well_zarr = image_zarr.parent
    plate_zarr = image_zarr.parents[2]
    label_zarr = image_zarr / "labels/label_DAPI"
    validate_schema(path=str(image_zarr), type="image")
    validate_schema(path=str(well_zarr), type="well")
    validate_schema(path=str(plate_zarr), type="plate")
    validate_schema(path=str(label_zarr), type="label")

    check_file_number(zarr_path=image_zarr)
