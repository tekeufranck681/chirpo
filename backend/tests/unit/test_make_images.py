from pathlib import Path

import pytest

from chirpo.core.images import sniff
from evals.scoring import IMAGES_DIR

pytest.importorskip("PIL")  # not a chirpo dependency: `uv run --with pillow pytest ...`

from evals import make_images


def test_the_script_draws_every_image_the_cases_use(tmp_path: Path) -> None:
    make_images.main(tmp_path)
    drawn = {path.name for path in tmp_path.iterdir()}
    committed = {path.name for path in IMAGES_DIR.iterdir()}
    assert drawn == committed
    for name in drawn:
        data = (tmp_path / name).read_bytes()
        assert sniff(data) == "image/png", name
        assert len(data) < 100_000, name
