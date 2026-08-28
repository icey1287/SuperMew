from __future__ import annotations

import os
import shutil
import time

import pytest

from backend.sandbox import SandboxExecutionSpec, SandboxLimits
from backend.sandbox.docker import DockerSandboxAdapter, DockerSandboxConfig


ENABLED = os.getenv("TEST_SANDBOX_DOCKER", "").strip() == "1"
pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="TEST_SANDBOX_DOCKER is not enabled",
)


@pytest.fixture(scope="module")
def adapter():
    image = os.getenv("TEST_SANDBOX_IMAGE", "").strip()
    binary = os.getenv("TEST_SANDBOX_DOCKER_BINARY", "docker").strip()
    docker_host = os.getenv("TEST_SANDBOX_DOCKER_HOST", "").strip() or None
    require_rootless = os.getenv("TEST_SANDBOX_REQUIRE_ROOTLESS", "1").strip() != "0"
    if not image:
        pytest.fail("TEST_SANDBOX_IMAGE is required when Docker integration is enabled")
    if shutil.which(binary) is None:
        pytest.fail("Docker binary is unavailable in required integration mode")
    runtime = DockerSandboxAdapter(
        config=DockerSandboxConfig(
            image=image,
            docker_binary=binary,
            docker_host=docker_host,
            require_rootless=require_rootless,
        )
    )
    # Explicit opt-in is a required mode: daemon or image failures must fail,
    # never become a second silent skip.
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.close()


def _spec(image: str, *, source: str) -> SandboxExecutionSpec:
    return SandboxExecutionSpec(
        invocation_id="sbx_" + ("9" * 32),
        identity_binding="8" * 64,
        language="python",
        source=source,
        image=image,
        limits=SandboxLimits(timeout_seconds=5, max_output_bytes=8192),
    )


def test_real_container_is_non_root_read_only_offline_and_env_minimal(adapter):
    source = """
import os
import socket

print(f"uid={os.getuid()}")
print(f"gid={os.getgid()}")
print(f"secret={os.getenv('SUPERMEW_HOST_SECRET')}")
try:
    open('/etc/supermew-probe', 'w').write('no')
except OSError:
    print('rootfs=readonly')
else:
    print('rootfs=writable')
try:
    socket.create_connection(('1.1.1.1', 53), timeout=0.5)
except OSError:
    print('network=blocked')
else:
    print('network=open')
""".strip()
    result = adapter.execute(
        _spec(adapter.config.image, source=source),
        deadline_at=time.monotonic() + 10,
        cancellation_probe=lambda: False,
    )

    assert "uid=0" not in result.stdout
    assert "gid=0" not in result.stdout
    assert "secret=None" in result.stdout
    assert "rootfs=readonly" in result.stdout
    assert "network=blocked" in result.stdout
    assert adapter.readiness()["active_containers"] == 0


def test_real_container_workspace_is_destroyed_between_invocations(adapter):
    first = adapter.execute(
        _spec(
            adapter.config.image,
            source="open('first.txt', 'w').write('private'); print('created')",
        ),
        deadline_at=time.monotonic() + 10,
        cancellation_probe=None,
    )
    second = adapter.execute(
        _spec(
            adapter.config.image,
            source="import os; print(os.path.exists('first.txt'))",
        ),
        deadline_at=time.monotonic() + 10,
        cancellation_probe=None,
    )

    assert first.files_created == 1
    assert second.stdout.strip() == "False"
    assert adapter.readiness()["active_containers"] == 0
