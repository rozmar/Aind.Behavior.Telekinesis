import datetime
import os

import aind_behavior_services.calibration.load_cells as lcc
import aind_behavior_services.rig as rig
import aind_behavior_telekinesis.task_logic as tl
from aind_behavior_services.calibration.aind_manipulator import (
    AindManipulatorCalibration,
    AindManipulatorCalibrationInput,
    AindManipulatorCalibrationOutput,
    Axis,
    AxisConfiguration,
    ManipulatorPosition,
    MotorOperationMode,
)
from aind_behavior_services.calibration.water_valve import (
    Measurement,
    WaterValveCalibration,
    WaterValveCalibrationInput,
    WaterValveCalibrationOutput,
)
from aind_behavior_services.session import AindBehaviorSessionModel
from aind_behavior_telekinesis.rig import (
    AindManipulatorDevice,
    AindTelekinesisRig,
    Networking,
    RigCalibration,
    ZmqConnection,
)


def mock_session() -> AindBehaviorSessionModel:
    """Generates a mock AindBehaviorSessionModel model"""
    return AindBehaviorSessionModel(
        date=datetime.datetime.now(tz=datetime.timezone.utc),
        experiment="Telekinesis",
        root_path="c://",
        subject="test",
        notes="test session",
        experiment_version="0.1.0",
        allow_dirty_repo=True,
        skip_hardware_validation=False,
        experimenter=["Foo", "Bar"],
    )


def mock_rig() -> AindTelekinesisRig:
    """Generates a mock AindVrForagingRig model"""

    manipulator_calibration = AindManipulatorCalibration(
        output=AindManipulatorCalibrationOutput(),
        input=AindManipulatorCalibrationInput(
            full_step_to_mm=(ManipulatorPosition(x=0.010, y1=0.010, y2=0.010, z=0.010)),
            axis_configuration=[
                AxisConfiguration(
                    axis=Axis.Y1, min_limit=-1, max_limit=18.5, motor_operation_mode=MotorOperationMode.QUIET
                ),
                AxisConfiguration(axis=Axis.X, min_limit=-1, max_limit=35),
                AxisConfiguration(axis=Axis.Z, min_limit=-1, max_limit=35),
            ],
            homing_order=[Axis.Y1, Axis.X, Axis.Z],
            initial_position=ManipulatorPosition(y1=0, y2=0, x=0, z=0),
        ),
    )

    water_valve_input = WaterValveCalibrationInput(
        measurements=[
            Measurement(valve_open_interval=1, valve_open_time=1, water_weight=[1, 1], repeat_count=200),
            Measurement(valve_open_interval=2, valve_open_time=2, water_weight=[2, 2], repeat_count=200),
        ]
    )
    water_valve_calibration = WaterValveCalibration(
        input=water_valve_input, output=water_valve_input.calibrate_output(), date=datetime.datetime.now()
    )
    water_valve_calibration.output = WaterValveCalibrationOutput(slope=1.0 / 20, offset=0)  # For testing purposes

    video_writer = rig.cameras.VideoWriterOpenCv(
        frame_rate=25,
        container_extension="avi",
    )

    load_cells_calibration = lcc.LoadCellsCalibration(
        output=lcc.LoadCellsCalibrationOutput(),
        input=lcc.LoadCellsCalibrationInput(),
        date=datetime.datetime.now(),
    )
    return AindTelekinesisRig(
        rig_name="BCI_Bonsai_i",
        triggered_camera_controller=rig.cameras.CameraController[rig.cameras.SpinnakerCamera](
            frame_rate=25,
            cameras={
                "MainCamera": rig.cameras.SpinnakerCamera(
                    serial_number="SerialNumber",
                    binning=2,
                    exposure=10000,
                    gain=20,
                    video_writer=video_writer,
                )
            },
        ),
        harp_load_cells=lcc.LoadCells(port_name="COM4", calibration=load_cells_calibration),
        harp_behavior=rig.harp.HarpBehavior(port_name="COM6"),
        harp_lickometer=rig.harp.HarpLicketySplit(port_name="COM8"),
        harp_clock_generator=rig.harp.HarpWhiteRabbit(port_name="COM3"),
        harp_analog_input=None,
        manipulator=AindManipulatorDevice(port_name="COM5", calibration=manipulator_calibration),
        calibration=RigCalibration(water_valve=water_valve_calibration),
        networking=Networking(
            zmq_publisher=ZmqConnection(connection_string="@tcp://localhost:5556", topic="Telekinesis")
        ),
        ophys_interface=None,
    )


def mock_task_logic() -> tl.AindTelekinesisTaskLogic:
    prototype_trial = tl.Action(
        reward_probability=tl.scalar_value(1),
        reward_amount=tl.scalar_value(1),
        reward_delay=tl.scalar_value(0),
        action_duration=tl.scalar_value(1),
        is_operant=False,
        time_to_collect=tl.scalar_value(5),
        lower_action_threshold=tl.scalar_value(0),
        upper_action_threshold=tl.scalar_value(20000),
        continuous_feedback=tl.ManipulatorFeedback(converter_lut_input=[0, 1], converter_lut_output=[1, 10]),
    )
    return tl.AindTelekinesisTaskLogic(
        task_parameters=tl.AindTelekinesisTaskParameters(
            rng_seed=None,
            environment=tl.Environment(
                block_statistics=[
                    tl.BlockGenerator(
                        block_size=tl.scalar_value(1000),
                        trial_statistics=tl.Trial(
                            inter_trial_interval=tl.scalar_value(10),
                            quiescence_period=None,
                            response_period=tl.ResponsePeriod(
                                duration=tl.scalar_value(10), has_cue=True, action=prototype_trial
                            ),
                            action_source_0=tl.LoadCellActionSource(channel=0),
                            action_source_1=tl.BehaviorAnalogInputActionSource(channel=0),
                            lut_reference="amazing_lut",
                        ),
                    )
                ],
            ),
            operation_control=tl.OperationControl(
                spout=tl.SpoutOperationControl(
                    default_retracted_position=10, default_extended_position=20, enabled=False
                ),
                action_luts={
                    "amazing_lut": tl.ActionLookUpTableFactory(
                        path="amazing_lut.tiff",
                        offset=0,
                        scale=1,
                        action0_min=0,
                        action0_max=20000,
                        action1_min=0,
                        action1_max=2048,
                    )
                },
            ),
        )
    )


def main(path_seed: str = "./local/{schema}.json"):
    example_session = mock_session()
    example_rig = mock_rig()
    example_task_logic = mock_task_logic()

    os.makedirs(os.path.dirname(path_seed), exist_ok=True)

    models = [example_task_logic, example_session, example_rig]

    for model in models:
        with open(path_seed.format(schema=model.__class__.__name__), "w", encoding="utf-8") as f:
            f.write(model.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
