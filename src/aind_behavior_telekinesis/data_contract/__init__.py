from pathlib import Path

from aind_behavior_services.session import Session
from contraqctor.contract import Dataset, DataStreamCollection
from contraqctor.contract.camera import Camera
from contraqctor.contract.csv import Csv
from contraqctor.contract.harp import (
    DeviceYmlByFile,
    HarpDevice,
)
from contraqctor.contract.json import PydanticModel, SoftwareEvents
from contraqctor.contract.mux import MapFromPaths

from aind_behavior_telekinesis import __semver__
from aind_behavior_telekinesis.rig import AindBehaviorTelekinesisRig
from aind_behavior_telekinesis.task_logic import AindBehaviorTelekinesisTaskLogic


def dataset(
    path: Path,
    version: str = __semver__,
) -> Dataset:
    """
    Creates a Dataset object for the Telekinesis experiment.
    This function constructs a hierarchical representation of the data streams collected
    during a Telekinesis experiment, including hardware device data, software events,
    and configuration files.
    Parameters
    ----------
    path : Path
        Path to the root directory containing the dataset
    version : str, optional
        Version of the dataset, defaults to the package version (This is also the version of the experiment)
    Returns
    -------
    Dataset
        A Dataset object containing a hierarchical representation of all data streams
        from the Telekinesis experiment, including:
        - Harp device data (behavior, load cells, manipulator, etc.)
        - Harp device commands
        - Software events (trials, rewards, task parameters)
        - Log files
        - Configuration schemas (rig, task logic, session)
    """
    name: str = "TelekinesisDataset"
    description: str = "A Telekinesis dataset"
    root_path = Path(path)
    return Dataset(
        name=name,
        version=version,
        description=description,
        data_streams=[
            DataStreamCollection(
                name="Behavior",
                description="Data from the Behavior modality",
                data_streams=[
                    HarpDevice(
                        name="HarpBehavior",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/Behavior.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    HarpDevice(
                        name="HarpManipulator",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/StepperDriver.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    HarpDevice(
                        name="HarpLickometer",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/Lickometer.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    HarpDevice(
                        name="HarpClockGenerator",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/ClockGenerator.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    HarpDevice(
                        name="HarpEnvironmentSensor",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/EnvironmentSensor.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    HarpDevice(
                        name="HarpAnalogInput",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/AnalogInput.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    HarpDevice(
                        name="HarpLoadCells",
                        reader_params=HarpDevice.make_params(
                            path=root_path / "behavior/LoadCells.harp",
                            device_yml_hint=DeviceYmlByFile(),
                        ),
                    ),
                    DataStreamCollection(
                        name="HarpCommands",
                        description="Commands sent to Harp devices",
                        data_streams=[
                            HarpDevice(
                                name="HarpBehavior",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/Behavior.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                            HarpDevice(
                                name="HarpManipulator",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/StepperDriver.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                            HarpDevice(
                                name="HarpLickometer",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/Lickometer.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                            HarpDevice(
                                name="HarpClockGenerator",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/ClockGenerator.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                            HarpDevice(
                                name="HarpEnvironmentSensor",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/EnvironmentSensor.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                            HarpDevice(
                                name="HarpAnalogInput",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/AnalogInput.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                            HarpDevice(
                                name="HarpLoadCells",
                                reader_params=HarpDevice.make_params(
                                    path=root_path / "behavior/HarpCommands/LoadCells.harp",
                                    device_yml_hint=DeviceYmlByFile(),
                                ),
                            ),
                        ],
                    ),
                    DataStreamCollection(
                        name="SoftwareEvents",
                        description="Software events generated by the workflow. The timestamps of these events are low precision and should not be used to align to physiology data.",
                        data_streams=[
                            SoftwareEvents(
                                name="TrialNumber",
                                description="Gets emitted at the start of each new trial with the trial number.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/TrialNumber.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="GiveReward",
                                description="An event emitted each time a reward event is produced. A reward event can be null if reward was sampled but not delivered. Otherwise it contains the reward volume delivered.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/GiveReward.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="ContinuousFeedbackProcess",
                                description="Continuous feedback process settings instantiated. Gets emitted each time a new process is requested.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/ContinuousFeedbackProcess.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="ActiveBlock",
                                description="An event emitted each time a new block is instantiated. It contains the block settings.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/ActiveBlock.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="QuiescencePeriod",
                                description="An event emitted each time a new quiescence period starts.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/QuiescencePeriod.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="ResponsePeriod",
                                description="An event emitted each time a new response period starts.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/ResponsePeriod.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="ChoiceMiss",
                                description="An event emitted each time a choice deadline is hit during the response period.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/ChoiceMiss.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="PerformedAction",
                                description="The action performed during the response period. Can be null if no action was registered.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/PerformedAction.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="IsValidRewardOutcome",
                                description="An event emitted each time a reward outcome is determined. It is false, if reward was contingent on the animal making a lick and it did not. True otherwise.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/IsValidRewardOutcome.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="IsValidTrial",
                                description="Whether the trial completed successfully or if it was aborted (e.g.: choice deadline).",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/IsValidTrial.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="Trial",
                                description="The trial settings that were used for the trial.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/Trial.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="Annotations",
                                description="Human-made annotations made during the session.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/Annotations.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="RepositoryStatus",
                                description="Repository status (if the repo was clean or not).",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/RepositoryStatus.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="RngSeed",
                                description="The random seed value used for the session.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/RngSeed.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="LoadCellCalibration",
                                description="An event emitted each time a load cell is calibrated.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/LoadCellCalibration.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="EndExperiment",
                                description="An event emitted if the experiment ended successfully.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/EndExperiment.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="ReferenceManipulatorPosition",
                                description="An event emitted with the latest reference manipulator position.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/ReferenceManipulatorPosition.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="StartSessionTime",
                                description="An event emitted at the start of the session. Data contains a UTC timestamp of the session start time.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/StartSessionTime.json"
                                ),
                            ),
                            SoftwareEvents(
                                name="EndSessionTime",
                                description="An event emitted at the end of the session. Data contains a UTC timestamp of the session end time.",
                                reader_params=SoftwareEvents.make_params(
                                    root_path / "behavior/SoftwareEvents/EndSessionTime.json"
                                ),
                            ),
                        ],
                    ),
                    DataStreamCollection(
                        name="OperationControl",
                        description="Streams associated with online operation of the task.",
                        data_streams=[
                            Csv(
                                "CurrentActionVector",
                                description="The abstracted action vector representing the current action being calculated. Timestamps are hardware derived using the Action0 samples that gave rise to the values.",
                                reader_params=Csv.make_params(
                                    path=root_path / "behavior/OperationControl/CurrentActionVector.csv",
                                    index="Seconds",
                                ),
                            ),
                            Csv(
                                "SpoutPosition",
                                description="The ongoing spout position throughout the session. Timestamps are software derived using the latest harp event. For hardware derived timestamps use the StepperDriver harp stream instead.",
                                reader_params=Csv.make_params(
                                    path=root_path / "behavior/OperationControl/SpoutPosition.csv",
                                    index="Seconds",
                                ),
                            ),
                        ],
                    ),
                    DataStreamCollection(
                        name="Configuration",
                        description="Configuration files for the behavior rig, task_logic and session.",
                        data_streams=[
                            PydanticModel(
                                name="Rig",
                                reader_params=PydanticModel.make_params(
                                    model=AindBehaviorTelekinesisRig,
                                    path=root_path / "behavior/Logs/rig_output.json",
                                ),
                            ),
                            PydanticModel(
                                name="TaskLogic",
                                reader_params=PydanticModel.make_params(
                                    model=AindBehaviorTelekinesisTaskLogic,
                                    path=root_path / "behavior/Logs/tasklogic_output.json",
                                ),
                            ),
                            PydanticModel(
                                name="Session",
                                reader_params=PydanticModel.make_params(
                                    model=Session,
                                    path=root_path / "behavior/Logs/session_output.json",
                                ),
                            ),
                        ],
                    ),
                ],
            ),
            MapFromPaths(
                name="BehaviorVideos",
                description="Data from BehaviorVideos modality",
                reader_params=MapFromPaths.make_params(
                    paths=root_path / "behavior-videos",
                    include_glob_pattern=["*"],
                    inner_data_stream=Camera,
                    inner_param_factory=lambda camera_name: Camera.make_params(
                        path=root_path / "behavior-videos" / camera_name
                    ),
                ),
            ),
        ],
    )


def render_dataset(version: str = __semver__) -> str:
    """Renders the dataset as a tree-like structure for visualization."""
    from contraqctor.contract.utils import print_data_stream_tree_html

    return print_data_stream_tree_html(dataset(Path("<RootPath>")), show_missing_indicator=False, show_type=True)
