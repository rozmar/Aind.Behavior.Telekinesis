#%% set up 2D map
import datetime
import os

import numpy as np
from PIL import Image
import matplotlib.pyplot as plt
LOCAL_ASSET_FOLDER = "C:\Scripts\Aind.Behavior.Telekinesis\local"

def gaussian_2d(x_lat,
                x_ap, 
                peak,
                trough,
                center_lat,
                center_ap, 
                sigma_lat, 
                sigma_ap):
    y =(peak-trough) * np.exp(-((x_lat[np.newaxis,:] - center_lat)**2 / (2 * sigma_lat**2) + (x_ap[:,np.newaxis] - center_ap)**2 / (2 * sigma_ap**2)))
    y= trough+y
    return y
# generate a speed matrix
bit_depth = 8 # image bit depth for speed LUT
bin_num_lat = 100 # lateral bin num
bin_num_ap = 100 # anterior-posterior bin num
lat_range = [-2000,2000] # these are lateral load cell readings
ap_range = [-2000,2000] # these are anterior-posterior load cell readings
speed_lookup = np.zeros([2**bit_depth,2])
speed_lookup[:,0] = np.arange(2**bit_depth)
speed_lookup[:,1] = np.round(np.arange(-1,1,2/(2**bit_depth))/5,2)*5
speed_parameters = {'ap':{'center':-750,
                         'width':200},
                    'lat':{'center':750,
                         'width':200},
                    'peak':5,
                    'trough':0}
lateral_force_vector  =np.arange(lat_range[0],lat_range[1],np.diff(lat_range)[0]/bin_num_lat)
ap_force_vector  =np.arange(ap_range[0],ap_range[1],np.diff(ap_range)[0]/bin_num_ap)
speed_matrix = gaussian_2d(lateral_force_vector,
                           ap_force_vector, 
                           speed_parameters['peak'],
                           speed_parameters['trough'],
                           speed_parameters['lat']['center'],
                           speed_parameters['ap']['center'],
                           speed_parameters['lat']['width'],
                           speed_parameters['ap']['width'])
speed_parameters['ap']['center'] = 750
speed_matrix_2 = gaussian_2d(lateral_force_vector,
                           ap_force_vector, 
                           speed_parameters['peak'],
                           speed_parameters['trough'],
                           speed_parameters['lat']['center'],
                           speed_parameters['ap']['center'],
                           speed_parameters['lat']['width'],
                           speed_parameters['ap']['width'])
speed_parameters['ap']['center'] = -750
speed_parameters['lat']['center'] = -750

speed_matrix_3 = gaussian_2d(lateral_force_vector,
                           ap_force_vector, 
                           speed_parameters['peak'],
                           speed_parameters['trough'],
                           speed_parameters['lat']['center'],
                           speed_parameters['ap']['center'],
                           speed_parameters['lat']['width'],
                           speed_parameters['ap']['width'])
speed_matrix = speed_matrix + speed_matrix_2 + speed_matrix_3 + speed_matrix.T - .05



#speed_matrix = np.abs(speed_matrix-np.max(speed_matrix))-.5
im = Image.fromarray(speed_matrix)
im.save(f"{LOCAL_ASSET_FOLDER}\\2d_gaussian.tiff")

# %
#!pip install matplotlib

import matplotlib.pyplot as plt
im = plt.imshow(speed_matrix, cmap='gray',extent=[ap_range[0],ap_range[1],lat_range[0],lat_range[1]])
plt.colorbar(im)

#%%
from aind_behavior_services.rig import cameras, harp
from aind_behavior_services.rig.aind_manipulator import (
    AindManipulatorCalibration,
    Axis,
    AxisConfiguration,
    ManipulatorPosition,
    MotorOperationMode,
)
from aind_behavior_services.rig.water_valve import Measurement, calibrate_water_valves
from aind_behavior_services.session import Session

import aind_behavior_telekinesis.task_logic as tl
from aind_behavior_telekinesis.rig import (
    AindBehaviorTelekinesisRig,
    AindManipulatorDevice,
    Networking,
    RigCalibration,
    ZmqConnection,
)
import aind_behavior_services.rig.load_cells as lcc
# parameters
rig_name = 'Behavior_0'
computer_name = os.getenv("COMPUTERNAME", "UnknownComputer")
save_folder_root = "C://Data"
session_notes = "second session, not a full day has passed"
experimenter = ["rozmar"]
Motor_Operation_Mode = MotorOperationMode.QUIET # .QUIET or .DYNAMIC
trial_length = 1000 #s
lick_response_time = 4 #s
inter_trial_interval = 2
reward_size = 1
#%
mouse_name = "TEST_MOUSE"
far_position = 5 #reward port starting position
close_position = 14.5 #reward port ending position
mouse_motor_hard_limit = close_position+.5# cannot come closer than that
#%%
def mock_session() -> Session:
    """Generates a mock Session model"""
    return Session(
        date=datetime.datetime.now(tz=datetime.timezone.utc),
        experiment="Isometric Task",
        subject=mouse_name,
        notes=session_notes,
        allow_dirty_repo=True,
        skip_hardware_validation=False,
        experimenter=experimenter,
    )


def mock_rig() -> AindBehaviorTelekinesisRig:
    """Generates a mock AindVrForagingRig model"""

    manipulator_calibration = AindManipulatorCalibration(
        full_step_to_mm=(ManipulatorPosition(x=0.010, y1=0.020, y2=0.020, z=0.010)),
        axis_configuration=[
            AxisConfiguration(
                axis=Axis.Y1, min_limit=-1, max_limit=mouse_motor_hard_limit, motor_operation_mode=Motor_Operation_Mode
            ),
            AxisConfiguration(axis=Axis.X, min_limit=-1, max_limit=21),
            AxisConfiguration(axis=Axis.Z, min_limit=-1, max_limit=21),
        ],
        homing_order=[Axis.Y1, Axis.X, Axis.Z],
        initial_position=ManipulatorPosition(y1=0, y2=0, x=0, z=0),
    )

    measurements = [
        Measurement(valve_open_interval=1, valve_open_time=1, water_weight=[1, 1], repeat_count=200),
        Measurement(valve_open_interval=2, valve_open_time=2, water_weight=[2, 2], repeat_count=200),
    ]
    water_valve_calibration = calibrate_water_valves(measurements)

    video_writer = cameras.VideoWriterFfmpeg()

    return AindBehaviorTelekinesisRig(
        rig_name=rig_name,
        computer_name=computer_name,
        data_directory=save_folder_root,
        triggered_camera_controller=cameras.CameraController[cameras.SpinnakerCamera](
            frame_rate=80,
            cameras={
                "MainCamera": cameras.SpinnakerCamera(
                    serial_number="25312141",
                    binning=2,
                    exposure=10000,
                    gain=18,
                    video_writer=video_writer,
                    adc_bit_depth=None,
                )
            },
        ),
        
        harp_load_cells=lcc.LoadCells(port_name="COM4"),
        harp_behavior=harp.HarpBehavior(port_name="COM3"),
        harp_lickometer=harp.HarpLicketySplit(port_name="COM6"),
        harp_clock_generator=harp.HarpWhiteRabbit(port_name="COM9"),
        harp_analog_input=None,
        manipulator=AindManipulatorDevice(port_name="COM10", calibration=manipulator_calibration, spout_axis=Axis.Y1),
        calibration=RigCalibration(water_valve=water_valve_calibration),
        networking=Networking(
            zmq_publisher=ZmqConnection(connection_string="@tcp://localhost:5556", topic="Telekinesis")
        ),
        ophys_interface=None,
    )


def mock_task_logic() -> tl.AindBehaviorTelekinesisTaskLogic:
    prototype_trial = tl.Action(
        reward_probability=tl.scalar_value(1),
        reward_amount=tl.scalar_value(reward_size),
        reward_delay=tl.scalar_value(0),
        action_duration=tl.scalar_value(.1),
        is_operant=False,
        time_to_collect=tl.scalar_value(lick_response_time),
        lower_action_threshold=tl.scalar_value(0),
        upper_action_threshold=tl.scalar_value(1),
        continuous_feedback=tl.ManipulatorFeedback(converter_lut_input=[0, 1], converter_lut_output=[far_position, close_position]),
    )
    return tl.AindBehaviorTelekinesisTaskLogic(
        task_parameters=tl.AindTelekinesisTaskParameters(
            rng_seed=None,
            environment=tl.Environment(
                block_statistics=[
                    tl.BlockGenerator(
                        block_size=tl.scalar_value(1000),
                        trial_statistics=tl.Trial(
                            inter_trial_interval=tl.scalar_value(inter_trial_interval),
                            quiescence_period=tl.QuiescencePeriod(duration=tl.scalar_value(0.5), action_threshold=1),
                            response_period=tl.ResponsePeriod(
                                duration=tl.scalar_value(trial_length), has_cue=True, action=prototype_trial
                            ),
                            #action_source_0=tl.BehaviorAnalogInputActionSource(channel=0),
                            action_source_0 = tl.LoadCellActionSource(channel=0),
                            action_source_1 = tl.LoadCellActionSource(channel=1),
                            sampler=tl.LutSampler2D(lut_reference="gaussian_2d"),
                            #sampler=tl.Sampler1D(min_from=0, max_from=3.3, min_to=0, max_to=1000),
                        ),
                    )
                ],
            ),
            operation_control=tl.OperationControl(
                action_luts={
                    "linear_normalized_to_1": tl.ActionLookUpTableFactory(
                        path="../examples/1d_ramp.tiff",
                        offset=0,
                        scale=1,
                        action0_max=5,  # Define input ranges here
                        action0_min=0,
                        action1_max=0,
                        action1_min=0,
                    ),
                    "linear_normalized_to_5": tl.ActionLookUpTableFactory(
                        path="../examples/1d_ramp.tiff",
                        offset=0,
                        scale=5,
                        action0_max=5,
                        action0_min=0,
                        action1_max=0,
                        action1_min=0,
                    ),
                    "1d_sampler": tl.ActionLookUpTableFactory(
                        path="../examples/minimal_2x1.tiff",
                        offset=0,
                        scale=5,
                        action0_max=5,
                        action0_min=0,
                        action1_max=0,
                        action1_min=0,
                    ),
                    "2d_sampler": tl.ActionLookUpTableFactory(
                        path="../examples/minimal_2x2.tiff",
                        offset=0,
                        scale=5,
                        action0_max=5,
                        action0_min=0,
                        action1_max=5,  # Define input ranges here
                        action1_min=0,
                    ),
                    "gaussian_2d": tl.ActionLookUpTableFactory(
                        path=f"{LOCAL_ASSET_FOLDER}/2d_gaussian.tiff",
                        offset=0,
                        scale=2.5,
                        action0_max=2000,
                        action0_min=-2000,
                        action1_max=2000,
                        action1_min=-2000,
                    ),
                },
                spout=tl.SpoutOperationControl(
                    default_retracted_position=far_position, default_extended_position=close_position, enabled=False
                ),
            ),
        )
    )


def main(path_seed: str = "./local/{schema}.json"):
    example_task_logic = mock_task_logic()
    example_session = mock_session()
    example_rig = mock_rig()

    os.makedirs(os.path.dirname(path_seed), exist_ok=True)

    models = [example_task_logic, example_session, example_rig]

    for model in models:
        with open(path_seed.format(schema=model.__class__.__name__), "w", encoding="utf-8") as f:
            f.write(model.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
