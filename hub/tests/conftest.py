import pytest

from homehub import config
from homehub.config import load_config, parse_config


@pytest.fixture(scope="session")
def example_config():
    return load_config(config.EXAMPLE_PATH)


@pytest.fixture
def private_config(example_config, tmp_path):
    import dataclasses

    paths = dataclasses.replace(example_config.paths, data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"))
    return dataclasses.replace(example_config, paths=paths, backup=dataclasses.replace(example_config.backup, dir=str(tmp_path / "backups")))


@pytest.fixture
def laptop_config(tmp_path):
    return parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}}, source=config.EXAMPLE_PATH)


@pytest.fixture
def temp_storage(tmp_path):
    from homehub.storage import Storage

    store = Storage(tmp_path / "homehub.db")
    yield store
    store.close()
