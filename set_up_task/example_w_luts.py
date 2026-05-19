import os

import aind_behavior_telekinesis.task_logic as tl

LOCAL_ASSET_FOLDER = "./local/"


def mock_task_logic() -> tl.AindBehaviorTelekinesisTaskLogic:
    trial_prototype = tl.Trial(
        inter_trial_interval=tl.scalar_value(2),
        quiescence_period=tl.QuiescencePeriod(duration=tl.scalar_value(0.5), action_threshold=500),
        response_period=tl.ResponsePeriod(
            duration=tl.scalar_value(10),
            action=tl.Action(
                reward_probability=tl.scalar_value(1),
                reward_amount=tl.scalar_value(3),
                reward_delay=tl.scalar_value(0),
                action_duration=tl.scalar_value(0.1),
                is_operant=True,
                time_to_collect=tl.scalar_value(1),
                lower_action_threshold=tl.scalar_value(0),
                upper_action_threshold=tl.scalar_value(10000),
                continuous_feedback=tl.ManipulatorFeedback(
                    converter_lut_input=[0, 1], converter_lut_output=[0, 15]
                ),  # This is in real manipulator units
            ),
        ),
        action_source_0=tl.BehaviorAnalogInputActionSource(channel=1),
        sampler=tl.LutSampler2D(lut_reference="linear_normalized_to_1"),
    )

    return tl.AindBehaviorTelekinesisTaskLogic(
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

    mat = np.array([np.linspace(0, 1, 100)], dtype=np.float32)
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

    # Minimal 2x2 LUT: 2D bilinear interpolation (Action0 x Action1)
    mat_2x2 = np.array(
        [[0.0, 0.5], [0.5, 1.0]],
        dtype=np.float32,
    )
    Image.fromarray(mat_2x2).save(f"{LOCAL_ASSET_FOLDER}/minimal_2x2.tiff")

    # Minimal 2x1 LUT: 1D linear interpolation (Action0 only)
    mat_2x1 = np.array(
        [[0.0], [1.0]],
        dtype=np.float32,
    )
    Image.fromarray(mat_2x1).save(f"{LOCAL_ASSET_FOLDER}/minimal_2x1.tiff")


def main(path_seed: str = "{LOCAL_ASSET_FOLDER}/{schema}.json"):
    example_task_logic = mock_task_logic()
    os.makedirs(os.path.dirname(path_seed).format(LOCAL_ASSET_FOLDER=LOCAL_ASSET_FOLDER, schema=""), exist_ok=True)
    generate_luts()

    models = [example_task_logic]

    for model in models:
        with open(
            path_seed.format(schema=model.__class__.__name__, LOCAL_ASSET_FOLDER=LOCAL_ASSET_FOLDER),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(model.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
