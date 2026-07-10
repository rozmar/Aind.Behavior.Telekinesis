import typing as t

import pandas as pd
from contraqctor import contract, qc
from contraqctor.contract.harp import HarpDevice

from aind_behavior_telekinesis.rig import AindBehaviorTelekinesisRig


class TelekinesisQcSuite(qc.Suite):
    def __init__(self, dataset: contract.Dataset):
        self.dataset = dataset

    def test_end_session_exists(self):
        """Check that the session has an end event."""
        end_session = self.dataset["Behavior"]["SoftwareEvents"]["EndExperiment"]

        if not end_session.has_data:
            return self.fail_test(
                None, "EndExperiment event does not exist. Session may be corrupted or not ended properly."
            )

        assert isinstance(end_session.data, pd.DataFrame)
        if end_session.data.empty:
            return self.fail_test(None, "No data in EndExperiment. Session may be corrupted or not ended properly.")
        else:
            return self.pass_test(None, "EndExperiment event exists with data.")

    def test_has_annotations(self):
        """Check that the session has annotations and surfaces them in the context if they exist."""
        annotations: pd.DataFrame | None
        try:
            annotations = self.dataset["Behavior"]["SoftwareEvents"]["Annotations"].read()
        except (FileNotFoundError, FileExistsError):
            annotations = None

        if annotations is None or annotations.empty:
            return self.pass_test(
                None, "No Annotations stream found. This may be expected if no manual annotations were made."
            )

        data = annotations["data"].to_dict()  #  this will be a series of strings
        return self.warn_test(None, "Annotations found", context=data)


def make_qc_runner(dataset: contract.Dataset) -> qc.Runner:
    """
    Create a QC runner with checks specific to the telekinesis task.

    Args:
        dataset: The loaded dataset to run QC checks on.

    Returns:
        A configured QC runner with all registered checks.
    """
    _runner = qc.Runner()
    dataset.load_all(strict=False)
    exclude: list[contract.DataStream] = []
    rig: AindBehaviorTelekinesisRig = dataset["Behavior"]["Configuration"]["Rig"].data

    # Exclude commands to Harp boards as these are tested separately
    for cmd in dataset["Behavior"]["HarpCommands"]:
        for stream in cmd:
            if isinstance(stream, contract.harp.HarpRegister):
                exclude.append(stream)

    # Optional Harp devices declared in the rig schema. If a device is not present in the
    # rig, it is skipped rather than tested against data that is not expected to exist.
    optional_harp_devices = {
        "HarpEnvironmentSensor": rig.harp_environment_sensor,
        "HarpAnalogInput": rig.harp_analog_input,
        "HarpLoadCells": rig.harp_load_cells,
    }

    # Add Harp tests for ALL Harp devices in the dataset
    for stream in (_r := dataset["Behavior"]):
        if isinstance(stream, HarpDevice):
            if stream.name in optional_harp_devices and optional_harp_devices[stream.name] is None:
                continue
            commands = t.cast(HarpDevice, _r["HarpCommands"][stream.name])
            _runner.add_suite(qc.harp.HarpDeviceTestSuite(stream, commands), stream.name)

    # Add Harp Hub tests
    _runner.add_suite(
        qc.harp.HarpHubTestSuite(
            dataset["Behavior"]["HarpClockGenerator"],
            [
                harp_device
                for harp_device in dataset["Behavior"]
                if isinstance(harp_device, HarpDevice)
                and not (harp_device.name in optional_harp_devices and optional_harp_devices[harp_device.name] is None)
            ],
        ),
        "HarpHub",
    )

    if rig.harp_environment_sensor is not None:
        _runner.add_suite(
            qc.harp.HarpEnvironmentSensorTestSuite(dataset["Behavior"]["HarpEnvironmentSensor"]),
            "HarpEnvironmentSensor",
        )

    _runner.add_suite(qc.harp.HarpLicketySplitTestSuite(dataset["Behavior"]["HarpLickometer"]), "HarpLickometer")

    # Add camera qc
    for camera in dataset["BehaviorVideos"]:
        _runner.add_suite(
            qc.camera.CameraTestSuite(camera, expected_fps=rig.triggered_camera_controller.frame_rate), camera.name
        )

    # Add Csv tests
    csv_streams = [stream for stream in dataset.iter_all() if isinstance(stream, contract.csv.Csv)]
    for stream in csv_streams:
        _runner.add_suite(qc.csv.CsvTestSuite(stream), stream.name)

    # Add the Telekinesis specific tests
    _runner.add_suite(TelekinesisQcSuite(dataset), "Telekinesis")
    return _runner
