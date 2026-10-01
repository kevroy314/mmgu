import pytest

from mmgu.core.hall import hall


@pytest.fixture(autouse=True)
def booted_hall():
    """Unit tests here read the module/permission registries, which exist only after boot."""
    if not hall.booted:
        hall.boot()
    assert len(hall.perms.perms) > 20 and len(hall.modules) > 5
    return hall
