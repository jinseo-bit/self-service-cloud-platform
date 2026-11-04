"""Derived image for the Serverless blueprint (D-36, D-166).

Lambda container images cannot use layers, so the Lambda Web Adapter is added as an image layer:
the user's image is left untouched, and a new image = user image + /opt/extensions/lambda-adapter is pushed
to the environment's own ECR repository. `crane` does this registry-to-registry, so the CodeBuild runner
needs no Docker daemon and stays unprivileged.
"""

from __future__ import annotations

import base64
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

PLATFORM = "linux/amd64"  # the blueprint's Lambda architecture is x86_64
ADAPTER_IN_IMAGE = "lambda-adapter"  # path of the binary inside public.ecr.aws/awsguru/aws-lambda-adapter
ADAPTER_TARGET = "opt/extensions/lambda-adapter"


class ImageBuildError(Exception):
    """A user-facing reason the derived image could not be built (bad image, pull failure, push failure)."""


def _crane(run, *args: str, stdin: str | None = None) -> str:
    p = run(["crane", *args], input=stdin, capture_output=True, text=True)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise ImageBuildError(f"crane {args[0]} failed: {tail[0][:300]}")
    return p.stdout.strip()


def _login(run, ecr, registry: str) -> None:
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    user, password = base64.b64decode(token).decode().split(":", 1)
    _crane(run, "auth", "login", registry, "-u", user, "--password-stdin", stdin=password)


def adapter_layer(adapter_bytes: bytes) -> bytes:
    """A single-layer tarball that places the adapter where Lambda loads extensions from."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for d in ("opt", "opt/extensions"):
            info = tarfile.TarInfo(d)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        info = tarfile.TarInfo(ADAPTER_TARGET)
        info.size, info.mode = len(adapter_bytes), 0o755
        tar.addfile(info, io.BytesIO(adapter_bytes))
    return buf.getvalue()


def build_derived_image(*, source: str, repository_url: str, tag: str, adapter_image: str, ecr,
                        run=subprocess.run) -> str:
    """Returns the pushed image pinned by digest: <repository_url>@sha256:..."""
    _login(run, ecr, repository_url.split("/", 1)[0])
    with tempfile.TemporaryDirectory() as d:
        export = Path(d) / "adapter.tar"
        _crane(run, "export", "--platform", PLATFORM, adapter_image, str(export))
        try:
            with tarfile.open(export) as tar:
                names = {m.name.lstrip("./"): m for m in tar.getmembers() if m.isfile()}
                adapter = tar.extractfile(names[ADAPTER_IN_IMAGE]).read()
        except (KeyError, tarfile.TarError, AttributeError) as e:
            raise ImageBuildError(f"adapter binary not found in {adapter_image}: {e}") from None
        layer = Path(d) / "layer.tar"
        layer.write_bytes(adapter_layer(adapter))
        target = f"{repository_url}:{tag}"
        try:
            _crane(run, "append", "--platform", PLATFORM, "-b", source, "-f", str(layer), "-t", target)
        except ImageBuildError as e:
            raise ImageBuildError(f"could not build from {source} ({e}). Check that the image exists, is public "
                                  f"and has a {PLATFORM} variant.") from None
        digest = _crane(run, "digest", target)
    if not digest.startswith("sha256:"):
        raise ImageBuildError(f"unexpected digest for {target}: {digest[:80]}")
    return f"{repository_url}@{digest}"


def copy_image(*, source: str, repository_url: str, tag: str, ecr, run=subprocess.run) -> str:
    """Container blueprint (D-170): the user's image is copied unchanged (its linux/amd64 variant) into the
    environment's ECR repository, so ECS runs a scanned, immutable, digest-pinned copy (SB-PUB-07) and never
    depends on the public registry at task start. Returns <repository_url>@sha256:..."""
    _login(run, ecr, repository_url.split("/", 1)[0])
    target = f"{repository_url}:{tag}"
    try:
        _crane(run, "copy", "--platform", PLATFORM, source, target)
    except ImageBuildError as e:
        raise ImageBuildError(f"could not copy {source} ({e}). Check that the image exists, is public "
                              f"and has a {PLATFORM} variant.") from None
    digest = _crane(run, "digest", target)
    if not digest.startswith("sha256:"):
        raise ImageBuildError(f"unexpected digest for {target}: {digest[:80]}")
    return f"{repository_url}@{digest}"
