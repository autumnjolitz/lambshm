import json
import os
import shutil
import types
import base64
import datetime
import builtins
import sys
import time
from contextlib import suppress, contextmanager
from invoke.exceptions import UnexpectedExit
from pathlib import Path
from typing import Literal, Type, Union, Iterable, Dict, Tuple, NamedTuple, Optional, List

from invoke.context import Context
from tasksupport import task, first, InvertedMapping, trim, truncate

_ = types.SimpleNamespace()
this = sys.modules[__name__]
AWS_LAMBDA_REPO = "public.ecr.aws/lambda/python"
DOCKER_PYTHON = "docker.io/python"
BASE_IMAGES: dict[str, str | None] = {
    # python:3.8
    f"{AWS_LAMBDA_REPO}:3.8": f"{AWS_LAMBDA_REPO}@sha256:a04abc05330a09c239c3e3d62408dd8331c5b3e3ee323a3d8a29cb0fad4d5356",
    # python:3.9
    f"{AWS_LAMBDA_REPO}:3.9": f"{AWS_LAMBDA_REPO}@sha256:696a74214bac1cf4afe6427331c6fc609c8b58a343f62e0ed9e3a483f120d1f1",
    f"{DOCKER_PYTHON}:3.9-bookworm": f"{DOCKER_PYTHON}@sha256:1fcc3e2d0128c39c20eb34e1a094c66f78cbea1de52428e0a5a82588bf0d50c7",
}
FLAVORS = {"apt-get": "debian", "yum": "redhat"}
BASE_IMAGES_BY_SHA = InvertedMapping(BASE_IMAGES)
IMAGE_DIGEST_CACHE_TTL: int | float = 5 * 60
EMPTY_MAPPING = {}
DEFAULT_FORMAT = "lines"


def _task_init():
    del globals()["_task_init"]
    root = _.project_root(Path, silent=True)
    for image_tag in BASE_IMAGES:
        override_filename = root / f".overrides.{_.b64encode(image_tag)}"
        if not override_filename.exists():
            continue
        if BASE_IMAGES[image_tag] is not None:
            with suppress(FileNotFoundError):
                os.remove(override_filename)
            continue
        with suppress(FileNotFoundError):
            with open(override_filename, "r") as fh:
                image_sha, ts = fh.read().strip().splitlines()
                ts = float(ts)
                ttl = ts - time.time()
                if ttl > 0:
                    print(
                        f"Loaded cached SHA ({image_sha!r}) for {image_tag!r} (TTL {ttl:.2f}). Please add a SHA to the BASE_IMAGES!",
                        file=sys.stderr,
                    )
                    BASE_IMAGES[image_tag] = image_sha
                    continue
                print(
                    f"Ignoring cached {image_sha!r} -- expired {ttl} seconds ago", file=sys.stderr
                )
                os.remove(override_filename)


class HashedImage(NamedTuple):
    repository: str
    type: str
    hash: str


class Image(NamedTuple):
    name: str
    tags: Tuple[str, ...]


def compose_environ(*, copy_os_environ: bool = False, **kwargs) -> Dict[str, str]:
    """
    Returns some common values for Docker builds
    """
    environment = {
        **(os.environ if copy_os_environ else EMPTY_MAPPING),
        "NO_COLOR": "1",
        "COMPOSE_DOCKER_CLI_BUILD": "1",
        "BUILDX_EXPERIMENTAL": "1",
        "BUILDX_GIT_LABELS": "full",
        "BUILDKIT_PROGRESS": "plain",
        "DOCKER_BUILDKIT": "1",
        "COMPOSE_PROJECT_NAME": "lambshm",
        **kwargs,
    }
    return environment


@contextmanager
def cd(path: str | Path):
    if not isinstance(path, Path):
        path = Path(path)
    new_cwd = path.resolve()
    prior_cwd = Path(os.getcwd()).resolve()
    try:
        os.chdir(new_cwd)
        yield prior_cwd
    finally:
        os.chdir(prior_cwd)


@task
def branch_name(context: Context) -> str:
    with suppress(KeyError):
        return os.environ["GITHUB_REF_NAME"]
    here = this._.project_root(Path, silent=True)
    if (here / ".git").is_dir():
        with suppress(FileNotFoundError):
            return context.run(f"git -C {here!s} branch --show-current", hide="both").stdout.strip()
        with open(here / ".git" / "HEAD") as fh:
            for line in fh:
                if line.startswith("ref:"):
                    _, line = (x.strip() for x in line.split(":", 1))
                if line.startswith("refs/heads/"):
                    return line.removeprefix("refs/heads/")
    raise ValueError("Unable to determine branch name!")


@task
def b64encode(value: str, silent: bool = True) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().strip()


@task
def b64decode(value: str, silent: bool = True) -> str:
    remainder = len(value) % 8
    if remainder:
        value += "=" * remainder
    return base64.urlsafe_b64decode(value).decode().strip()


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
    for base_image in BASE_IMAGES_BY_SHA:
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
def digests_for(context: Context, image_name: str, *, silent: bool = False) -> tuple[str, ...]:
    image_name_prefix = image_name
    if ":" in image_name_prefix:
        image_name_prefix, _ = image_name_prefix.split(":", 1)
    fh = context.run(
        "docker inspect --format='{{.RepoDigests}}' %s" % (image_name,),
        hide="both" if silent else None,
        warn=True,
    )
    if "No such object:" in fh.stderr:
        return ()
    digests = trim(fh.stdout.strip(), "[]").split()
    if not silent:
        digests_friendly = ", ".join(digests) or "No digests found!"
        print(f"Digests for {image_name}: {digests_friendly}")
    return tuple(x for x in digests if x.startswith(image_name_prefix))


@task
def download(context: Context, /, silent: bool = False, build_ref: str = "") -> Tuple[Image, ...]:
    downloaded: List[Image] = []
    for image_sha in BASE_IMAGES_BY_SHA:
        if image_sha is None:
            continue
        context.run(
            f"docker pull {image_sha}", env=compose_environ(), hide="both" if silent else None
        )
        tags = BASE_IMAGES_BY_SHA[image_sha:image_sha]
        for tag in tags:
            if not silent:
                print(f"Tagging {image_sha} -> {tag}")
            context.run(f"docker tag {image_sha} {tag}", hide="both" if silent else None)
        downloaded.append(Image(image_sha, tuple(tags)))
    for image_tag in BASE_IMAGES:
        if BASE_IMAGES[image_tag] is None:
            context.run(
                f"docker pull {image_tag}", env=compose_environ(), hide="both" if silent else None
            )
            image_sha = _.cached_digest_for(context, image_tag, silent=True)
            if not silent:
                print(f"Assigning {image_sha} to {image_tag} for this run", file=sys.stderr)
            BASE_IMAGES[image_tag] = image_sha
    for our_image in _.all_image_names(context):
        image_tag = this._.suggest_image_tag(context, build_ref, silent=True)
        for tag in (
            image_tag,
            f"commit.{_.commit_sha(context)}",
            f"commit.{_.commit_sha(context, -1)}",
        ):
            uri = _.show_image_uri_for(context, our_image, build_ref=build_ref, tag=tag)
            result = context.run(f"docker pull {uri}", warn=True)
            if not result:
                print(f"Unable to pull {uri}: {result.stderr}.", file=sys.stderr)
    return tuple(downloaded)


@task
def cached_digest_for(
    context: Context,
    image_tag: str,
    expires_in: float | int = IMAGE_DIGEST_CACHE_TTL,
    silent: bool = False,
) -> str:
    root = _.project_root(Path, silent=True)
    file = root / f".overrides.{_.b64encode(image_tag)}"
    with suppress(FileNotFoundError):
        with open(file) as fh:
            image_sha, t_s = fh.read().strip().splitlines()
            t_s = float(t_s)
            ttl = t_s - time.time()
            if ttl < expires_in:
                return image_sha
            # expired!
            if not silent:
                print(f"Removing {file!r}, renewing digests!", file=sys.stderr)
            os.remove(file)
    (image_sha,) = _.digests_for(context, image_tag, silent=True)
    with open(file, "w") as fh:
        fh.write(f"{image_sha}\n{time.time() + expires_in!s}\n")
    return image_sha


@task()
def our_image_name_for(
    context: Context,
    /,
    base_image: Optional[str] = None,
    skip_tag: List[str] = ["latest", "head", "main", "master"],
    all: bool = False,
) -> str:
    """
    Given a base image, return the expected patched output name
    """
    if base_image is None:
        base_image = first(BASE_IMAGES_BY_SHA)
    image_tags = this._.get_tags_from(context, base_image, silent=True)
    results = []
    skip_tag = frozenset(skip_tag)
    for base_image in image_tags:
        if "/" in base_image:
            _, image = base_image.rsplit("/", 1)
        else:
            image = base_image
        tag_name = ""
        with suppress(ValueError):
            image, tag_name = image.split(":")
            if tag_name in skip_tag:
                continue
        if tag_name:
            if tag_name[0].isdigit():
                image = f"lambshm/{image}{tag_name}"
            else:
                image = f"lambshm/{image}-{tag_name}"
        if not all:
            return image
        results.append(image)
    if not all and not results:
        raise FileNotFoundError(f"Unable to find images for {base_image}")
    return tuple(results)


@task
def all_image_names(
    context: Context,
) -> Tuple[str, ...]:
    """
    List all the expected image names given the BASE_IMAGES
    """
    images = []
    for base_image in BASE_IMAGES_BY_SHA:
        images.append(our_image_name_for(context, base_image, silent=True))
    return tuple(images)


@task
def get_flavor_for(context, image_sha):
    for package_manager in FLAVORS:
        try:
            result = context.run(
                f'docker run --rm --entrypoint /bin/sh -t {image_sha} -c "{package_manager} --help"',
                hide="both",
            )
        except UnexpectedExit:
            continue
        else:
            return FLAVORS[package_manager]
    raise LookupError


@task
def build(
    context,
    runtime: bool = True,
    tests: bool = True,
    silent: bool = False,
    override_image_name: Optional[str] = None,
) -> Tuple[str, ...]:
    """
    Patch the images to have a writeable libc shm_open(2) directory compatible with Python
    """
    now = datetime.datetime.utcnow().astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    images = []
    for base_image_by_digest in BASE_IMAGES_BY_SHA:
        (base_image_name,) = BASE_IMAGES_BY_SHA[base_image_by_digest:base_image_by_digest]
        if override_image_name is not None and base_image_name != override_image_name:
            print(f"{base_image_name} != {override_image_name}, skipping", file=sys.stderr)
            continue
        image_name = _.our_image_name_for(context, base_image_by_digest, silent=True)
        flavor = _.get_flavor_for(context, base_image_by_digest, silent=True)
        if runtime:
            if not silent:
                print("Building runtime image", file=sys.stderr)
            path = (
                "docker compose --ansi never "
                "-f config/docker-compose.yml "
                "build "
                f"--build-arg BASE_IMAGE={base_image_name} "
                f"--build-arg TODAY={now} "
                f"--build-arg BASE_IMAGE_DIGEST={base_image_by_digest} "
                "runtime"
            )
            if not silent:
                print(f"Running {path!r} {image_name!r}", file=sys.stderr)
            context.run(
                path,
                env=compose_environ(IMAGE_NAME=image_name, FLAVOR=flavor),
                hide=("both" if silent else None),
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
                env=compose_environ(IMAGE_NAME=image_name, FLAVOR=flavor),
                hide=("both" if silent else None),
            )
            test_image_name = f"{image_name}-test"
            images.append(test_image_name)
    return tuple(images)


@task
def test(
    context: Context,
    as_server: bool = False,
    silent: bool = False,
    override_image_name: Optional[str] = None,
) -> bool:
    """
    Run a test that should just pass. If it doesn't, it means the image is borked

    returns if it passes the test
    """
    for image_sha in BASE_IMAGES_BY_SHA:
        image_name = _.our_image_name_for(context, image_sha, silent=True)
        if override_image_name is not None and image_name != override_image_name:
            print(f"{image_name} != {override_image_name}, skipping", file=sys.stderr)
            continue
        flavor = _.get_flavor_for(context, image_sha, silent=True)
        env = compose_environ(IMAGE_NAME=image_name, FLAVOR=flavor)
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
        if not result:
            return False
    return True


@task
def list_local_images(
    context: Context, /, show: Literal["test", "runtime", "both"] = "both"
) -> Tuple[str, ...]:
    if show not in ("test", "runtime", "both"):
        raise SystemExit(f'Invalid show mode {show!r} - try one of {{"test", "both", "runtime"}} ')
    images = []
    result = context.run("docker image ls --format '{{ .Repository}}' lambshm/*", hide="both")
    for image in (x.strip() for x in result.stdout.splitlines() if x.strip()):
        if image.endswith("-test"):
            if show in ("both", "test"):
                images.append(image)
            continue
        if show in ("both", "runtime"):
            images.append(image)
    return tuple(images)


@task
def repository_owner(context: Context, /) -> str | None:
    owner = None
    with suppress(KeyError):
        owner = os.environ["GITHUB_ACTOR"]
    if not owner:
        with cd(this._.project_root(silent=True)):
            git_repo_url = context.run("git remote get-url origin", hide="stdout")
            if git_repo_url:
                _, short_repo_url = git_repo_url.stdout.split(":", 1)
                owner, _ = short_repo_url.split("/", 1)
    return owner


@task
def upload(context: Context, /, build_ref: str = "", silent: bool = False, owner: str = "") -> None:
    if not owner:
        owner = _.repository_owner(context, silent=True)
        print(f"owner inferred to be {owner!r}")
    if not owner:
        raise ValueError("Unable to determine owner of repository, please specify an owner!")
    images = list_local_images(context, "runtime")
    image_tag = this._.suggest_image_tag(context, build_ref, silent=True)
    if images:
        t_s = time.time()
        print(f"Begin upload of {len(images)} images")
        for image in images:
            _.upload_image(
                context, image, image_tag, silent=silent, owner=owner, build_ref=build_ref
            )
            commit_sha = this._.commit_sha(context)
            if commit_sha:
                _.upload_image(
                    context,
                    image,
                    f"commit.{commit_sha}",
                    silent=silent,
                    owner=owner,
                    build_ref=build_ref,
                )
        print(f"Uploaded {len(images)} images in {time.time() - t_s:.2f} seconds")


@task
def commit_sha(context: Context, /, depth: int = 0):
    with suppress(KeyError):
        return os.environ["GITHUB_SHA"]
    here = this._.project_root(Path, silent=True)
    if (here / ".git").is_dir():
        with suppress(FileNotFoundError):
            fh = context.run(f"git -C {here!s} rev-parse HEAD~{abs(depth)}", hide="both", warn=True)
            if fh:
                return fh.stdout.strip()
    raise ValueError("Unable to deduce commit_sha!")


@task
def build_ref(context: Context, /, ref: str = "", silent: bool = True) -> str | None:
    if not ref:
        with suppress(KeyError):
            ref = os.environ["GITHUB_REF"]
    if not ref:
        ref = f"refs/heads/{_.branch_name(context, silent=TabError)}"
        print(f"ref inferred from branch_name to be {ref}", file=sys.stderr)
    if not ref.startswith("refs/"):
        raise ValueError(f'References (ref) must start with "ref/", not {truncate(ref, 10)!r}')
    return ref


@task
def suggest_image_tag(context: Context, /, ref: str = "") -> str:
    """
    Attempt to deduce the probable image tag name using the current build references available.
    """
    ref = _.build_ref(context, ref)
    if ref is None:
        return None
    if ref.startswith("refs/tags/"):
        tag_name = ref.removeprefix("refs/tags/").translate({ord("/"): ord("-")})
        assert tag_name.lower() not in ("main", "latest", "master")
        image_tag = f"tags.{tag_name}"
    elif ref.startswith("refs/heads/"):
        branch_name = ref.removeprefix("refs/heads/").translate({ord("/"): ord("-")})
        if branch_name == "main":
            image_tag = "latest"
        else:
            image_tag = f"branch.{branch_name}"
    elif ref.startswith("refs/pull/"):
        pull_request: int = int(ref.removeprefix("refs/pull/").removesuffix("/merge"), 10)
        image_tag = f"pr.{pull_request}"
    else:
        raise ValueError(f"Unrecognized ref {ref!r}")
    return truncate(image_tag, 128, trailer="")


@task
def upload_image(
    context: Context,
    /,
    image: str,
    tag: str,
    owner: str = "",
    silent: bool = False,
    build_ref: str = "",
) -> None:
    if not owner:
        owner = _.repository_owner(context, silent=True)
    if not owner:
        raise ValueError("Unable to determine owner of repository, please specify an owner!")
    image_repository = _.show_image_uri_for(context, image, owner, build_ref, tag)
    if not silent:
        print(f"Uploading {image} to {image_repository}", file=sys.stderr)
    t_s = time.time()
    context.run(f"docker tag {image} {image_repository}", hide="both" if silent else None)
    context.run(f"docker push {image_repository}", hide="both" if silent else None)
    if not silent:
        print(
            f"Uploaded {image} to {image_repository} in {time.time() - t_s:.2f}s", file=sys.stderr
        )


@task
def show_image_uri_for(
    context: Context, /, local_image: str, owner: str = "", build_ref: str = "", tag: str = ""
) -> str:
    """
    Deduce the fully qualified image repository url for a given image.

    owner: the repostory organization/owner. If empty, will deduce from the origin url.
    build_ref: the build reference. If empty, will deduce from the branch.
    """
    digests = _.digests_for(context, local_image, silent=True)
    if digests:
        # Not a local build!
        return this._.get_tags_from(context, digests[0], silent=True)[0]
    # local build!
    if not owner:
        owner = _.repository_owner(context, silent=True)
    if not owner:
        raise ValueError("Unable to determine owner of repository, please specify an owner!")
    build_ref = this._.build_ref(context, build_ref, silent=True)
    if not tag:
        tag = this._.suggest_image_tag(context, build_ref, silent=True)
    if tag:
        return f"ghcr.io/{owner}/{local_image}:{tag}"
    raise ValueError(f"Unable to deduce image uri for {local_image!r}")


@task
def list_containers_using(
    context: Context, /, image_id: str, silent: bool = False
) -> Tuple[str, ...]:
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


_task_init()
