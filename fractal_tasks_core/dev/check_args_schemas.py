"""
This script checks that JSON schemas for task arguments (as reported in the
package manfest) are up-to-date.
"""
import json
from pathlib import Path

import fractal_tasks_core
from .lib_args_schemas import create_schema_for_single_task


if __name__ == "__main__":

    # Read manifest
    manifest_path = (
        Path(fractal_tasks_core.__file__).parent / "__FRACTAL_MANIFEST__.json"
    )
    with manifest_path.open("r") as f:
        manifest = json.load(f)

    # Set or check global properties of manifest
    if not manifest["has_args_schemas"]:
        raise ValueError(f'{manifest["has_args_schemas"]=}')
    if manifest["args_schema_version"] != "pydantic_v1":
        raise ValueError(f'{manifest["args_schema_version"]=}')

    # Loop over tasks and set or check args schemas
    task_list = manifest["task_list"]
    for ind, task in enumerate(task_list):
        executable = task["executable"]
        print(f"[{executable}] Start")
        try:
            schema = create_schema_for_single_task(executable)
        except AttributeError:
            print(f"[{executable}] Skip, due to AttributeError")
            print()
            continue

        current_schema = task["args_schema"]
        if not current_schema == schema:
            raise ValueError("Schemas are different.")
        print("Schema in manifest is up-to-date.")
        print()
