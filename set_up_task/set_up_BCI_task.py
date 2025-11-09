#%%
import datetime
import os

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
from aind_behavior_services.calibration import load_cells as lcc
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

LOCAL_ASSET_FOLDER = "C:\git\Aind.Behavior.Telekinesis\local"#"./local/"
# parameters
save_folder_root = "D://Bonsai Data"
mouse_name = "test_subject"
session_notes = "test session"
experimenter = ["Marton Rozsa"]
Motor_Operation_Mode = MotorOperationMode.QUIET # .QUIET or .DYNAMIC
mouse_motor_hard_limit = 18 # cannot come closer than that
far_position = 9.5 #reward port starting position
close_position = 17.5 #reward port ending position

trial_length = 10 #s
lick_response_time = 2 #s



def mock_session() -> AindBehaviorSessionModel:
    """Generates a mock AindBehaviorSessionModel model"""
    return AindBehaviorSessionModel(
        date=datetime.datetime.now(tz=datetime.timezone.utc),
        experiment="Telekinesis",
        root_path=save_folder_root,
        subject=mouse_name,
        notes=session_notes,
        experiment_version="0.1.0",
        allow_dirty_repo=True,
        skip_hardware_validation=False,
        experimenter=experimenter,
    )


def mock_rig() -> AindTelekinesisRig:
    """Generates a mock AindVrForagingRig model"""

    manipulator_calibration = AindManipulatorCalibration(
        output=AindManipulatorCalibrationOutput(),
        input=AindManipulatorCalibrationInput(
            full_step_to_mm=(ManipulatorPosition(x=0.010, y1=0.010, y2=0.010, z=0.010)),
            axis_configuration=[
                AxisConfiguration(
                    axis=Axis.Y1, min_limit=-1, max_limit=mouse_motor_hard_limit, motor_operation_mode=Motor_Operation_Mode
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
            cameras={},
            # {
            #     "MainCamera": rig.cameras.SpinnakerCamera(
            #         serial_number="23381093",
            #         binning=2,
            #         exposure=10000,
            #         gain=20,
            #         video_writer=video_writer,
            #         adc_bit_depth=None,
            #     )
            # },
        ),
        harp_behavior=rig.harp.HarpBehavior(port_name="COM7"),#done
        harp_lickometer=rig.harp.HarpLicketySplit(port_name="COM11"),#done
        harp_clock_generator=rig.harp.HarpWhiteRabbit(port_name="COM10"),#done
        harp_load_cells=lcc.LoadCells(port_name="COM8", calibration=load_cells_calibration),#done
        harp_analog_input=None,
        manipulator=AindManipulatorDevice(port_name="COM9", calibration=manipulator_calibration),#done
        calibration=RigCalibration(water_valve=water_valve_calibration),
        networking=Networking(
            zmq_publisher=ZmqConnection(connection_string="@tcp://localhost:5556", topic="Telekinesis")
        ),
        ophys_interface=None,
    )



def mock_task_logic() -> tl.AindTelekinesisTaskLogic:
    trial_prototype = tl.Trial(
        inter_trial_interval=tl.scalar_value(2),
        quiescence_period=tl.QuiescencePeriod(duration=tl.scalar_value(0.5), action_threshold=10),
        response_period=tl.ResponsePeriod(
            duration=tl.scalar_value(trial_length),
            action=tl.Action(
                reward_probability=tl.scalar_value(1),
                reward_amount=tl.scalar_value(3),
                reward_delay=tl.scalar_value(0),
                action_duration=tl.scalar_value(0.1),
                is_operant=True,
                time_to_collect=tl.scalar_value(lick_response_time),
                lower_action_threshold=tl.scalar_value(0),
                upper_action_threshold=tl.scalar_value(1),
                continuous_feedback=tl.ManipulatorFeedback(converter_lut_input=[0, 1], converter_lut_output=[far_position, close_position]),
            ),
        ),
        action_source_0=tl.BehaviorAnalogInputActionSource(channel=1),
        lut_reference="linear_normalized_to_1",
    )
    return tl.AindTelekinesisTaskLogic(
        task_parameters=tl.AindTelekinesisTaskParameters(
            environment=tl.Environment(
                block_statistics=[
                    tl.BlockGenerator(block_size=tl.scalar_value(9999), trial_statistics=trial_prototype)
                ],
                repeat_count=None,
                shuffle=False,
            ),
            operation_control=tl.OperationControl(
                action_luts={
                    "linear_normalized_to_1": tl.ActionLookUpTableFactory(
                        path=f"{LOCAL_ASSET_FOLDER}/1d_ramp.tiff",
                        offset=0,
                        scale=1,
                        action0_max=3.3,
                        action0_min=0,
                        action1_max=1,
                        action1_min=0,
                    ),
                   "linear_normalized_to_5": tl.ActionLookUpTableFactory(
                        path=f"../{LOCAL_ASSET_FOLDER}/1d_ramp.tiff",
                        offset=0,
                        scale=5,
                        action0_max=5,
                        action0_min=0,
                        action1_max=1,
                        action1_min=0,
                    ),
                },
                spout=tl.SpoutOperationControl(
                    default_retracted_position=0, default_extended_position=10, enabled=True
                ),
            ),
        )
    )


def generate_luts() -> None:
    import numpy as np
    from PIL import Image

    mat = np.array([np.linspace(0, 1000, 100)], dtype=np.float32)
    im = Image.fromarray(mat)
    im.save(f"{LOCAL_ASSET_FOLDER}/1d_ramp.tiff")

    mat = np.zeros((100, 100), dtype=np.float32)
    c = (x / 2 for x in mat.shape)
    thr = (x / 4 for x in mat.shape)
    idx = [abs(np.arange(0, x) - c) > t for x, c, t in zip(mat.shape, c, thr)]
    idx_ = idx[0][:, np.newaxis] & idx[1]
    mat[idx_] = 1
    im = Image.fromarray(mat)
    im.save(f"{LOCAL_ASSET_FOLDER}/2d_outer_squares.tiff")


def main(path_seed: str = "{LOCAL_ASSET_FOLDER}/{schema}.json"):
    example_session = mock_session()
    example_rig = mock_rig()
    example_task_logic = mock_task_logic()
    os.makedirs(os.path.dirname(path_seed).format(LOCAL_ASSET_FOLDER=LOCAL_ASSET_FOLDER, schema=""), exist_ok=True)
    generate_luts()

    models = [example_task_logic, example_session, example_rig]

    for model in models:
        with open(
            path_seed.format(schema=model.__class__.__name__, LOCAL_ASSET_FOLDER=LOCAL_ASSET_FOLDER),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(model.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
