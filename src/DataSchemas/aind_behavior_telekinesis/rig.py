# Import core types
from __future__ import annotations

# Import core types
from typing import Annotated, Literal, Optional, Union

import aind_behavior_services.calibration.load_cells as lcc
import aind_behavior_services.calibration.water_valve as wvc
import aind_behavior_services.rig as rig
from aind_behavior_services.calibration import aind_manipulator
from aind_behavior_services.rig import AindBehaviorRigModel
from pydantic import BaseModel, Field
from typing_extensions import TypeAliasType

__version__ = "0.2.0"


class AindManipulatorAdditionalSettings(BaseModel):
    """Additional settings for the manipulator device"""

    spout_axis: aind_manipulator.Axis = Field(default=aind_manipulator.Axis.Y1, description="Spout axis")


class AindManipulatorDevice(aind_manipulator.AindManipulatorDevice):
    """Overrides the default settings for the manipulator device by spec'ing additional_settings field"""

    additional_settings: AindManipulatorAdditionalSettings = Field(
        default=AindManipulatorAdditionalSettings(), description="Additional settings"
    )


class RigCalibration(BaseModel):
    water_valve: wvc.WaterValveCalibration = Field(default=..., description="Water valve calibration")


class ZmqConnection(BaseModel):
    connection_string: str = Field(default="@tcp://localhost:5556")
    topic: str = Field(default="")


class Networking(BaseModel):
    zmq_publisher: ZmqConnection = Field(
        default=ZmqConnection(connection_string="@tcp://localhost:5556", topic="telekinesis"), validate_default=True
    )
    zmq_subscriber: Literal[None] = Field(default=None)


class _OphysInterfaceBase(BaseModel):
    interface: str


class BergamoInterface(_OphysInterfaceBase):
    interface: Literal["bergamo"] = "bergamo"
    delay_trial: float = Field(default=0.0, ge=0, description="Arbitrary delay between start trigger and trial start")


class Slap2pInterface(_OphysInterfaceBase):
    interface: Literal["slap2p"] = "slap2p"
    delay_trial: float = Field(default=0.0, ge=0, description="Arbitrary delay between start trigger and trial start")
    delay_ready_start: float = Field(
        default=0.2, ge=0, description="Delay between the system being ready and a start signal being issued"
    )
    timeout_for_error: float = Field(
        default=5,
        ge=0,
        description="Time to wait for the ready signal to go low after start. If it doesn't, an error is raised.",
    )


OphysInterface = TypeAliasType(
    "OphysInterface",
    Annotated[Union[BergamoInterface, Slap2pInterface], Field(discriminator="interface")],
)


class AindTelekinesisRig(AindBehaviorRigModel):
    version: Literal[__version__] = __version__
    triggered_camera_controller: rig.cameras.CameraController[rig.cameras.SpinnakerCamera] = Field(
        ..., description="Required camera controller to triggered cameras."
    )
    harp_behavior: rig.harp.HarpBehavior = Field(..., description="Harp behavior")
    harp_lickometer: rig.harp.HarpLicketySplit = Field(..., description="Harp lickometer")
    harp_load_cells: Optional[lcc.LoadCells] = Field(default=None, description="Harp load cells")
    harp_clock_generator: rig.harp.HarpTimestampGeneratorGen3 = Field(..., description="Harp clock generator")#HarpWhiteRabbit
    harp_analog_input: Optional[rig.harp.HarpAnalogInput] = Field(default=None, description="Harp analog input")
    harp_environment_sensor: Optional[rig.harp.HarpEnvironmentSensor] = Field(
        default=None, description="Harp environment sensor"
    )
    manipulator: AindManipulatorDevice = Field(..., description="Manipulator")
    calibration: RigCalibration = Field(default=..., description="General rig calibration")
    networking: Networking = Field(default=Networking(), description="Networking settings", validate_default=True)
    ophys_interface: Optional[OphysInterface] = Field(default=None, description="Ophys interface")
