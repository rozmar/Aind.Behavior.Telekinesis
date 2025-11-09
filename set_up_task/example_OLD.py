import datetime
import os

import aind_behavior_force_foraging.task_logic as task_logic
import aind_behavior_services.calibration.load_cells as lcc
import aind_behavior_services.rig as rig
import aind_behavior_services.task_logic.distributions as distributions
from aind_behavior_force_foraging.rig import AindForceForagingRig, AindManipulatorDevice, HarpLoadCells, RigCalibration
from aind_behavior_force_foraging.task_logic import (
    AindForceForagingTaskLogic,
    AindForceForagingTaskParameters,
)
from aind_behavior_services import db_utils as db
from aind_behavior_services.calibration.aind_manipulator import (
    AindManipulatorCalibration,
    AindManipulatorCalibrationInput,
    AindManipulatorCalibrationOutput,
    Axis,
    AxisConfiguration,
    ManipulatorPosition,
    MotorOperationMode
)
from aind_behavior_services.calibration.water_valve import (
    Measurement,
    WaterValveCalibration,
    WaterValveCalibrationInput,
    WaterValveCalibrationOutput,
)
from aind_behavior_services.session import AindBehaviorSessionModel

# mouse_name = "test"
# y_hard_limit = 33
# far_position = 10
# close_position = 18
# LUT_file = 'speed_LUT4.tiff'
# upper_force_threshold = 200
# endlimits = 10000


mouse_name = "PDCO-mouse"
y_hard_limit = 18
far_position = 9.5
close_position = 17.5
LUT_file = 'speedLUT_north.tiff'
upper_force_threshold = 25
endlimits = 1250
return_force_threshold = 0
watervolume = 3
trial_integration_time = 999
offset = -1.25





mouse_name = "BCI81"
y_hard_limit = 18
far_position = 9.5
close_position = 17.5
LUT_file = 'speedLUT_west.tiff'
upper_force_threshold = 25
endlimits = 1250
return_force_threshold = 2.5
watervolume = 3
trial_integration_time = 999
offset = -1



mouse_name = "BCI78"
y_hard_limit = 18
far_position = 9.5
close_position = 17.5
LUT_file = 'speedLUT_west.tiff'
upper_force_threshold = 20
endlimits = 1250
return_force_threshold = 5
watervolume = 3
trial_integration_time = 999
offset = -.5


# mouse_name = "BCI75"
# y_hard_limit = 17.5
# far_position = 9
# close_position = 17
# LUT_file = 'speedLUT_north_long.tiff'
# upper_force_threshold = 50
# endlimits = 2500
# return_force_threshold = 0
# watervolume = 3
# trial_integration_time = 999
# offset = -1

# mouse_name = "BCI71"
# y_hard_limit = 18
# far_position = 9.5
# close_position = 17.5
# LUT_file = 'speedLUT_north_long.tiff'
# upper_force_threshold = 25
# endlimits = 1250
# return_force_threshold = 0
# watervolume = 3
# trial_integration_time = 999
# offset = -.5


# mouse_name = "BCI83"
# y_hard_limit = 17.5
# far_position = 9
# close_position = 17
# LUT_file = 'speed_LUT6_flip.tiff'
# upper_force_threshold = 100
# endlimits = 2500
# return_force_threshold = 5
# watervolume = 3
# trial_integration_time = 999

# mouse_name = "BCI70"
# y_hard_limit = 17.5
# far_position = 9
# close_position = 17
# LUT_file = 'speed_LUT6_flip.tiff'
# upper_force_threshold = 100
# endlimits = 2000
# return_force_threshold = 5
# watervolume = 3
# trial_integration_time = 999

# mouse_name = "BCI72"
# y_hard_limit = 18.5
# far_position = 10
# close_position = 18
# LUT_file = 'speed_LUT1.tiff'
# upper_force_threshold = 250
# endlimits = 10000
# return_force_threshold = 5
# watervolume = 3
# trial_integration_time = 999


# mouse_name = "BCI84"
# y_hard_limit = 18.5
# far_position = 10
# close_position = 18
# LUT_file = 'speed_LUT4.tiff'
# upper_force_threshold = 400
# endlimits = 10000
# return_force_threshold = 5
# watervolume = 3
# trial_integration_time = 999

def mock_session() -> AindBehaviorSessionModel:
    """Generates a mock AindBehaviorSessionModel model"""
    return AindBehaviorSessionModel(
        date=datetime.datetime.now(tz=datetime.timezone.utc),
        experiment="ForceForaging",
        root_path="F://Bonsai-data//",#"c://",
        remote_path=None,
        subject=mouse_name,
        notes="test session",
        experiment_version="0.1.0",
        allow_dirty_repo=True,
        skip_hardware_validation=False,
        experimenter=["Marton Rozsa"],
    )


def mock_rig() -> AindForceForagingRig:
    """Generates a mock AindVrForagingRig model"""

    manipulator_calibration = AindManipulatorCalibration(
        output=AindManipulatorCalibrationOutput(),
        input=AindManipulatorCalibrationInput(
            full_step_to_mm=(ManipulatorPosition(x=0.010, y1=0.010, y2=0.010, z=0.010)),
            axis_configuration=[
                AxisConfiguration(axis=Axis.Y1, min_limit=-1, max_limit=y_hard_limit, motor_operation_mode=MotorOperationMode.QUIET),#DYNAMIC
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
        input=water_valve_input, output=water_valve_input.calibrate_output(), calibration_date=datetime.datetime.now()
    )
    water_valve_calibration.output = WaterValveCalibrationOutput(slope=1.0/20, offset=0)  # For testing purposes

    video_writer = rig.VideoWriterOpenCv(
        frame_rate=25,
        container_extension="avi",
    )

    load_cells_calibration = lcc.LoadCellsCalibration(
        output=lcc.LoadCellsCalibrationOutput(),
        input=lcc.LoadCellsCalibrationInput(),
        date=datetime.datetime.now(),
    )

    return AindForceForagingRig(
        rig_name="BCI_Bonsai_i",
        triggered_camera_controller=rig.CameraController[rig.SpinnakerCamera](
            frame_rate=25,
            cameras={
                "MainCamera": rig.SpinnakerCamera(
                    serial_number="23118308", binning=2, exposure=10000, gain=20, video_writer=video_writer,
                )
            },
        ),
        harp_load_cells=HarpLoadCells(port_name="COM4", calibration=load_cells_calibration),
        monitoring_camera_controller=None,
        harp_behavior=rig.HarpBehavior(port_name="COM6"),
        harp_lickometer=rig.HarpLickometer(port_name="COM8"),
        harp_clock_generator=rig.HarpClockGenerator(port_name="COM3"),
        manipulator=AindManipulatorDevice(port_name="COM5", calibration=manipulator_calibration),
        screen=rig.Screen(display_index=0),
        calibration=RigCalibration(water_valve=water_valve_calibration),
    )


def mock_task_logic() -> AindForceForagingTaskLogic:
    """Generates a mock AindVrForagingTaskLogic model"""

    
    force_op_control=task_logic.ForceOperationControl(
            press_mode=task_logic.PressMode.SINGLE_LOOKUP_TABLE,
            left_index=0,
            right_index=1,
            force_lookup_table= task_logic.ForceLookUpTable(
                path="F://Bonsai-task//{}".format(LUT_file),
                left_max=endlimits, left_min=-endlimits, right_max=endlimits, right_min=-endlimits,offset=offset,
            )
    )
    
    
    operation_control = task_logic.OperationControl(
        force=force_op_control,
        spout=task_logic.SpoutOperationControl(
            default_extended_position=close_position, default_retracted_position=far_position, enabled=True
        ),
    )

    def ExponentialDistributionHelper(rate=1, minimum=0, maximum=1000):
        return distributions.ExponentialDistribution(
            distribution_parameters=distributions.ExponentialDistributionParameters(rate=rate),
            truncation_parameters=distributions.TruncationParameters(min=minimum, max=maximum, is_truncated=True),
            scaling_parameters=distributions.ScalingParameters(scale=1.0, offset=0.0),
        )

    def ScalarDistributionHelper(value=1):
        return distributions.Scalar(
            distribution_parameters=distributions.ScalarDistributionParameter(value=value),
            truncation_parameters=None,
            scaling_parameters=None,
        )

    agent_environment = task_logic.Environment(
        block_statistics=[
            task_logic.BlockGenerator(
                is_baited=False,
                block_size=ScalarDistributionHelper(999),
                trial_statistics=task_logic.Trial(
                    inter_trial_interval=3,
                    quiescence_period=task_logic.QuiescencePeriod(duration=0.5, force_threshold=return_force_threshold),
                    initiation_period=task_logic.InitiationPeriod(duration=0, abort_on_force=False, has_cue=False),
                    response_period=task_logic.ResponsePeriod(duration=trial_integration_time, has_cue=True, has_feedback=False),
                    left_harvest=task_logic.HarvestAction(
                        action=task_logic.HarvestActionLabel.LEFT,
                        trial_type=task_logic.TrialType.ACCUMULATION,
                        probability=1.0,
                        amount=watervolume,
                        delay=0.0,
                        force_duration=0.01,
                        upper_force_threshold=upper_force_threshold,
                        lower_force_threshold=0,
                        is_operant=False,
                        time_to_collect=20.0,
                        continuous_feedback=task_logic.ManipulatorFeedback(converter_lut_input=[0, 1], converter_lut_output=[far_position, close_position])
                    ),
                    right_harvest=None,
                ),
            )
        ],
        repeat_count=None,
        shuffle=True,
    )
    return AindForceForagingTaskLogic(
        task_parameters=AindForceForagingTaskParameters(
            rng_seed=None, operation_control=operation_control, environment=agent_environment
        )
    )


def mock_subject_database() -> db.SubjectDataBase:
    """Generates a mock database object"""
    database = db.SubjectDataBase()
    database.add_subject("test", db.SubjectEntry(task_logic_target="preward_intercept_stageA"))
    database.add_subject("test2", db.SubjectEntry(task_logic_target="does_notexist"))
    return database


def main(path_seed: str = "F://Bonsai-task//task0//{schema}.json"):
    example_session = mock_session()
    example_rig = mock_rig()
    example_task_logic = mock_task_logic()
    example_database = mock_subject_database()

    os.makedirs(os.path.dirname(path_seed), exist_ok=True)
    models = [example_task_logic, example_session, example_rig, example_database]
    
    for model in models:
        
        with open(path_seed.format(schema=model.__class__.__name__), "w", encoding="utf-8") as f:
            f.write(model.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
