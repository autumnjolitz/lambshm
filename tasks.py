import json
import os
import shutil
import types
import pprint
import datetime
import builtins
import sys
from invoke.exceptions import UnexpectedExit
from typing import Literal, Type, Union, Iterable, Dict, Tuple, NamedTuple

from pathlib import Path
from invoke.context import Context
from contextlib import suppress

from tasksupport import task, first

_ = types.SimpleNamespace()
this = sys.modules[__name__]
AWS_LAMBDA_REPO = "public.ecr.aws/lambda/python"
BASE_IMAGES = {
    # python:3.8
    AWS_LAMBDA_REPO: f"{AWS_LAMBDA_REPO}@sha256:a04abc05330a09c239c3e3d62408dd8331c5b3e3ee323a3d8a29cb0fad4d5356",
}


_EMPTY_MAPPING = {}


class HashedImage(NamedTuple):
    repository: str
    type: str
    hash: str


def compose_environ(*, copy_os_environ: bool = False, **kwargs) -> Dict[str, str]:
    """
    Returns some common values for Docker builds
    """
    environment = {
        **(os.environ if copy_os_environ else _EMPTY_MAPPING),
        "NO_COLOR": "1",
        "COMPOSE_DOCKER_CLI_BUILD": "1",
        "BUILDX_EXPERIMENTAL": "1",
        "BUILDX_GIT_LABELS": "full",
        "BUILDKIT_PROGRESS": "plain",
        "DOCKER_BUILDKIT": "1",
        **kwargs,
    }
    return environment


@task
def project_root(
    type: Union[Type[str], Type[Path], Literal["str", "Path"]] = "str"
) -> Union[str, Path]:
    """
    Get the absolute path of the project root assuming tasks.py is in the repo root.
    """
    if isinstance(type, builtins.type):
        type = type.__name__
    assert type in ("str", "Path"), f"{type} may be str or Path"
    root = Path(__file__).resolve().parent
    if type == "str":
        return str(root)
    return root


@task
def python_path(
    type_name: Literal["str", "Path", str, Path] = "str",
    *,
    skip_venv: bool = False,
) -> Union[str, Path]:
    """
    Return the best python to use
    """
    if isinstance(type_name, type):
        type_name = type_name.__name__
    assert type_name in ("Path", "str")
    root = Path(__file__).resolve().parent
    python = root / "python" / "bin" / "python"
    if not python.exists():
        with suppress(KeyError):
            python = Path(os.environ["VIRTUAL_ENV"]) / "bin" / "python"
    if skip_venv or not python.exists():
        python = Path(
            shutil.which("python3"),
            path=":".join(x for x in os.environ["PATH"].split(":") if Path(x) != python.parent),
        ).resolve(True)
    if type_name == "str":
        return str(python)
    return python


@task
def setup(context: Context, python_bin: Union[str, None] = None, swap_venv_stage=None) -> Path:
    """
    Create the venv for this project.

    This task can destroy the project's venv and recreate it from the same process id.

    swap_venv_stage: This is the internals of how a venv can replace itself while depending only
    on the utilities within it (i.e. invoke). We pass the
    """
    root = _.project_root(Path)
    venv = root / "python"
    if python_bin is None:
        python_bin = _.python_path(str)

    if swap_venv_stage == "1-copy-new-venv":
        print(f"Removing old venv at {venv}")
        shutil.rmtree(root / "python")
        context.run(f"{venv!s}_/bin/python -m venv --copies {venv!s}")
        context.run(
            f"{venv!s}/bin/python -m pip install -r requirements.txt -r dev-requirements.txt"
        )
        os.execve(
            f"{venv!s}/bin/python",
            ("python", "-m", "invoke", "setup", "--swap-venv-stage", "2-remove-tmp-venv"),
            os.environ,
        )
        assert False, "unreachable!"
    if swap_venv_stage == "2-remove-tmp-venv":
        tmp_venv = root / "python_"
        print(f"Removing temp venv {tmp_venv}")
        shutil.rmtree(tmp_venv)
        original_argv = []
        try:
            original_argv = json.loads(os.environ["_LAMBSHM_ORIG_ARGS"])
        except ValueError:
            print("Unable to decode original _LAMBSHM_ORIG_ARGS!", file=sys.stderr)
        while original_argv and original_argv[0] == "--":
            del original_argv[0]
        print("Attempting to restore argv after setup which is", original_argv)
        if not original_argv:
            return
        os.execve(f"{venv!s}/bin/python", ("python", "-m", "invoke", *original_argv), os.environ)
        assert False, "unreachable!"

    current_python = Path(sys.executable)
    with suppress(FileNotFoundError):
        shutil.rmtree(f"{venv!s}_")
    if venv.exists() and str(current_python).startswith(str(venv)):
        # ARJ: Complex path: replacing a running environment.
        # Time for the os.execve hat dance!
        # make the subenvironment
        print(f"installing tmp venv at {venv!s}_")
        context.run(f"{python_bin} -m venv {venv!s}_", hide="both")
        with Path(root / "dev-requirements.txt").open("rb") as fh:
            for line in fh:
                line_st = line.strip()
                while b"#" in line_st:
                    line_st = line[: line_st.rindex(b"#")].strip()
                if not line_st:
                    continue
                if line.startswith(b"invoke"):
                    break
            else:
                line = b"invoke"
            print(f"installing tmp venv invoke")
            context.run(f"{venv!s}_/bin/python -m pip install {line.decode()}", hide="both")

        args = []
        skip_if_args = 0
        task_executed = True
        for arg in sys.argv:
            if task_executed and arg == "setup":
                skip_if_args += 2
                task_executed = False
                continue
            if arg == "--" or not arg.startswith("-"):
                skip_if_args = 0
                if arg == "--":
                    continue
            elif skip_if_args:
                skip_if_args -= 1
                continue
            if task_executed is False:
                args.append(arg)
        os.environ["_LAMBSHM_ORIG_ARGS"] = json.dumps(args)
        os.execve(
            f"{venv!s}_/bin/python",
            ("python", "-m", "invoke", "setup", "--swap-venv-stage", "1-copy-new-venv"),
            os.environ,
        )
        assert False, "unreachable"
    # Happy path:
    with suppress(FileNotFoundError):
        shutil.rmtree(root / "python")
    context.run(f"{python_bin} -m venv {venv!s}")
    context.run(f"{venv!s}/bin/python -m pip install -r requirements.txt -r dev-requirements.txt")
    return venv


@task
def get_tags_from(context: Context, image_name: str) -> Iterable[str]:
    """
    Given an image url, return the repo tags
    """
    try:
        result = context.run(f"docker inspect {image_name}", hide="both")
    except UnexpectedExit as e:
        if "Error: No such object:" in e.result.stderr:
            context.run(f"docker pull {image_name}", env=compose_environ())
            result = context.run(f"docker inspect {image_name}", hide="both")
        else:
            raise
    image = json.loads(result.stdout)
    results = []
    for match in image:
        results.extend(match["RepoTags"])
    return results


@task
def split_image_hash(image_name: str) -> HashedImage:
    """
    Given a docker hash image url, return HashedImage(repository, hash_type, hash)
    """
    image_name, hash_ = image_name.split("@", 1)
    type_, hash_ = hash_.split(":", 1)
    return HashedImage(image_name, type_, hash_)


@task
def all_source_image_names(context, silent: bool = False) -> Tuple[str, ...]:
    """
    List the source image friendly names
    """
    all_images = []
    for base_image in BASE_IMAGES.values():
        with suppress(ValueError):
            image, hash_function, value = _.split_image_hash(context, base_image)
            if not silent:
                print(f"Looking up tags for {image}@{hash_function}:{value}", file=sys.stderr)
            images = get_tags_from(context, base_image, silent=True)
            if not silent:
                print(f"-> {images}", file=sys.stderr)
            all_images.extend(images)
            continue
        all_images.append(base_image)
    return all_images


@task
def download(context: Context):
    for key, value in BASE_IMAGES.items():
        context.run(f"docker pull {value}", env=compose_environ())
        context.run(f"docker tag {value} {key}")


@task
def image_name(context: Context, base_image: str) -> str:
    """
    Given a base image, return the expected patched output name
    """
    (base_image, *_) = this._.get_tags_from(context, base_image, silent=True)
    _, image = base_image.rsplit("/", 1)
    image = image.translate({ord(":"): None})
    image = f"lambshm/{image}"
    return image


@task
def all_image_names(
    context: Context,
) -> Tuple[str, ...]:
    """
    List all the expected image names given the BASE_IMAGES
    """
    images = []
    for base_image in BASE_IMAGES.values():
        images.append(image_name(context, base_image, silent=True))
    return tuple(images)


@task
def build(
    context, runtime: bool = True, tests: bool = True, silent: bool = False
) -> Tuple[str, ...]:
    """
    Patch the images to have a writeable libc shm_open(2) directory compatible with Python
    """
    now = datetime.datetime.utcnow().astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    images = []
    for base_image in BASE_IMAGES.values():
        image_name = _.image_name(context, base_image, silent=True)
        if runtime:
            if not silent:
                print("Building runtime image", file=sys.stderr)
            context.run(
                "docker compose --ansi never "
                "-f config/docker-compose.yml "
                "build "
                f"--build-arg BASE_IMAGE={base_image} "
                f"--build-arg TODAY={now} "
                "runtime",
                env=compose_environ(IMAGE_NAME=image_name),
                hide="both" if silent else None,
            )
            images.append(image_name)
        if tests:
            if not silent:
                print("Building test image", file=sys.stderr)
            context.run(
                "docker compose --ansi never "
                "-f config/docker-compose.yml -f config/docker-compose.test.yml "
                "build "
                f"--build-arg BASE_IMAGE={image_name} "
                f"--build-arg TODAY={now} "
                "runtime",
                env=compose_environ(IMAGE_NAME=image_name),
                hide="both" if silent else None,
            )
            images.append(f"{image_name}-test")
    return tuple(images)


@task
def test(context: Context, as_server: bool = False, silent: bool = False) -> bool:
    """
    Run a test that should just pass. If it doesn't, it means the image is borked

    returns if it passes the test
    """
    image_name = _.image_name(context, first(BASE_IMAGES.values()), silent=True)
    env = compose_environ(IMAGE_NAME=image_name)
    if as_server:
        result = context.run(
            "docker compose --ansi never "
            "-f config/docker-compose.yml -f config/docker-compose.test.yml "
            "run --rm "
            "runtime ",
            env=env,
            hide="both" if silent else None,
        )
    else:
        result = context.run(
            "docker compose --ansi never "
            "-f config/docker-compose.yml -f config/docker-compose.test.yml "
            "run --rm --entrypoint /bin/sh "
            "runtime "
            "-c 'mkdir /tmp/shm && python lambda_handler.py'",
            env=env,
            hide="both" if silent else None,
        )
    if result:
        return True
    return False


@task
def list_local_images(context: Context) -> Tuple[str, ...]:
    result = context.run("docker image ls --format '{{ .Repository}}' lambshm/*", hide="both")
    image_ids = [x.strip() for x in result.stdout.splitlines() if x.strip()]
    return tuple(image_ids)


@task
def list_containers_using(context: Context, image_id: str, silent: bool = False) -> Tuple[str, ...]:
    format = '--format "{{.ID}}"'
    result = context.run(
        f"docker container ls --all --filter=ancestor='{image_id}' {format}", hide="both"
    )
    container_ids = [x.strip() for x in result.stdout.splitlines() if x.strip()]
    return tuple(container_ids)


@task
def clean(context: Context, silent: bool = False) -> None:
    """
    Removes all artifacts.
    """
    image_ids = list_local_images(context, silent=True)
    containers = []
    for image in image_ids:
        containers.extend(list_containers_using(context, image, silent=True))
    if containers:
        containers = " ".join(containers)
        context.run(f"docker rm -f {containers}", hide="both")
    if image_ids:
        image_ids = " ".join(image_ids)
        context.run(f"docker rmi -f {image_ids}", hide="both")
    context.run("docker builder prune -f", hide="both")
