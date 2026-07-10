import logging
from pathlib import Path
from typing import Any, cast

from aind_behavior_services.rig.aind_manipulator import ManipulatorPosition
from aind_behavior_services.session import Session
from clabe import resource_monitor, ui
from clabe.apps import (
    AindBehaviorServicesBonsaiApp,
)
from clabe.launcher import Launcher, LauncherCliArgs, experiment
from clabe.pickers import ByAnimalModifier, DefaultBehaviorPicker, DefaultBehaviorPickerSettings
from contraqctor.contract.json import SoftwareEvents
from pydantic_settings import CliApp

from aind_behavior_telekinesis import data_contract
from aind_behavior_telekinesis.rig import AindBehaviorTelekinesisRig
from aind_behavior_telekinesis.task_logic import AindBehaviorTelekinesisTaskLogic

logger = logging.getLogger(__name__)


@experiment()
async def telekinesis_experiment(launcher: Launcher) -> None:
    # Start experiment setup
    picker = DefaultBehaviorPicker(
        launcher=launcher,
        settings=DefaultBehaviorPickerSettings(
            config_library_dir=r"\\allen\aind\scratch\AindBehavior.db\AindTelekinesis"
        ),
    )

    session = picker.pick_session(Session)
    task_logic = picker.pick_task(AindBehaviorTelekinesisTaskLogic)
    rig = picker.pick_rig(AindBehaviorTelekinesisRig)
    ensure_rig_and_computer_name(rig)

    launcher.register_session(session, rig.data_directory)

    resource_monitor.ResourceMonitor(
        constrains=[
            resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e10),
        ]
    ).run()

    # Post-fetching modifications
    manipulator_modifier = ByAnimalManipulatorModifier(
        subject_db_path=picker.subject_dir / session.subject,
        model_path="manipulator.calibration.initial_position",
        model_name="manipulator_init.json",
        launcher=launcher,
    )
    manipulator_modifier.inject(rig)

    bonsai_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./src/main.bonsai"),
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task_logic,
    )
    await bonsai_app.run_async()
    # Update manipulator initial position for next session
    try:
        manipulator_modifier.dump()
    except Exception as e:
        logger.error(f"Failed to update manipulator initial position: {e}")
        launcher.frontend.notify(f"Failed to update manipulator position: {e}", ui.MessageLevel.WARNING)

    # Run data qc
    if picker.frontend.prompt_confirm(
        ui.ConfirmRequest(label="Would you like to generate a qc report?", default=False)
    ):
        try:
            import webbrowser

            from contraqctor.qc.reporters import HtmlReporter

            from aind_behavior_telekinesis.data_qc.data_qc import make_qc_runner

            vr_dataset = data_contract.dataset(launcher.session_directory)
            runner = make_qc_runner(vr_dataset)
            qc_path = launcher.session_directory / "Behavior" / "Logs" / "qc_report.html"
            reporter = HtmlReporter(output_path=qc_path)
            runner.run_all_with_progress(reporter=reporter)
            webbrowser.open(qc_path.as_uri(), new=2)
        except Exception as e:
            logger.error(f"Failed to run data QC: {e}")
            picker.frontend.notify(f"Failed to run data QC: {e}", ui.MessageLevel.ERROR)

    # Transfer data
    # is_transfer = picker.frontend.prompt_confirm(
    #     ui.ConfirmRequest(label="Would you like to transfer data?", default=True)
    # )
    # if not is_transfer:
    #    logger.info("Data transfer skipped by user.")
    #    return

    launcher.copy_logs()
    # RobocopyService(source=launcher.session_directory, settings=RobocopySettings()).transfer()
    return


def ensure_rig_and_computer_name(rig: AindBehaviorTelekinesisRig) -> None:
    """Ensures rig and computer name are set from environment variables if available, otherwise defaults to rig configuration values."""

    import os

    rig_name = os.environ.get("aibs_comp_id", None)
    computer_name = os.environ.get("hostname", None)

    if rig_name is None:
        logger.warning(
            "'aibs_comp_id' environment variable not set. Defaulting to rig name from configuration. %s", rig.rig_name
        )
        rig_name = rig.rig_name
    if computer_name is None:
        computer_name = rig.computer_name
        logger.warning(
            "'hostname' environment variable not set. Defaulting to computer name from configuration. %s",
            rig.computer_name,
        )

    if rig_name != rig.rig_name or computer_name != rig.computer_name:
        logger.warning(
            "Rig name or computer name from environment variables do not match the rig configuration. "
            "Forcing rig name: %s and computer name: %s from environment variables.",
            rig_name,
            computer_name,
        )
        rig.rig_name = rig_name
        rig.computer_name = computer_name


class ByAnimalManipulatorModifier(ByAnimalModifier[AindBehaviorTelekinesisRig]):
    """Modifier to set and update manipulator initial position based on animal-specific data."""

    def __init__(
        self, subject_db_path: Path, model_path: str, model_name: str, *, launcher: Launcher, **kwargs
    ) -> None:
        super().__init__(subject_db_path, model_path, model_name, **kwargs)
        self._launcher = launcher

    def _process_before_dump(self) -> ManipulatorPosition:
        _dataset = data_contract.dataset(self._launcher.session_directory)
        manipulator_init_position: SoftwareEvents = cast(
            SoftwareEvents, _dataset["Behavior"]["SoftwareEvents"]["ReferenceManipulatorPosition"].load()
        )
        data: dict[str, Any] = manipulator_init_position.data.iloc[-1]["data"]
        position = ManipulatorPosition.model_validate(data)
        return position


class ClabeCli(LauncherCliArgs):
    def cli_cmd(self):
        launcher = Launcher(settings=self)
        launcher.run_experiment(telekinesis_experiment)
        return None


def main() -> None:
    CliApp().run(ClabeCli)


if __name__ == "__main__":
    main()
