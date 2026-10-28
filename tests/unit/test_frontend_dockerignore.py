"Verify the actual Dockerfile-specific context with synthetic files only."

import os
import shutil
import subprocess
from pathlib import Path


def test_frontend_context_excludes_nested_private_and_generated_files(tmp_path):
    context = tmp_path / "context"
    frontend = context / "frontend"
    frontend.mkdir(parents=True)
    project = Path(__file__).resolve().parents[2]
    (frontend / "Dockerfile.dockerignore").write_text(
        (project / "frontend/Dockerfile.dockerignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (frontend / "Dockerfile").write_text(
        "FROM scratch\nCOPY frontend /snapshot\n", encoding="utf-8"
    )
    samples = {
        "src/app.ts": "synthetic public source",
        "package.json": "{}",
        ".env.local": "synthetic secret",
        "src/feature/.env.production.local": "synthetic secret",
        "src/feature/.git/config": "synthetic git metadata",
        "src/feature/.cache/session.json": "synthetic cache",
        "src/feature/.data/accounts.json": "synthetic private data",
        "src/feature/.tools/token.txt": "synthetic private data",
        "src/feature/.venv/secret.txt": "synthetic environment",
        "src/feature/node_modules/x/generated.js": "synthetic dependency",
        "src/feature/dist/generated.js": "synthetic build output",
        "src/feature/lower-client.pem": "synthetic certificate",
        "src/feature/upper-client.PEM": "synthetic certificate",
        "src/feature/lower-service.key": "synthetic private key",
        "src/feature/upper-service.KEY": "synthetic private key",
        "public/client.p12": "synthetic certificate bundle",
        "src/feature/credentials.production.json": "synthetic credentials",
        "src/feature/Credentials.dev.JSON": "synthetic credentials",
        "public/accounts-private.json": "synthetic accounts",
        "src/feature/Accounts-local.JSON": "synthetic accounts",
        "src/feature/credentials.ts": "synthetic public source",
        "src/accounting.json": "{}",
    }
    for relative, contents in samples.items():
        file = frontend / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(contents, encoding="utf-8")
    docker = shutil.which("docker")
    if not docker and os.name == "nt":
        root = Path(os.environ["INSIGHTPILOT_WORKSPACE_ROOT"])
        candidate = root / ".tools/docker/resources/bin/docker.exe"
        docker = str(candidate) if candidate.is_file() else None
    assert docker, "Docker CLI and BuildKit are required for this context test"
    out = tmp_path / "out"
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    result = subprocess.run(
        [
            docker,
            "buildx",
            "build",
            "--network=none",
            "--progress=plain",
            "--output",
            f"type=local,dest={out}",
            "-f",
            str(frontend / "Dockerfile"),
            str(context),
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, "Synthetic BuildKit build failed: " + result.stdout[-3000:]
    copied = {
        file.relative_to(out / "snapshot").as_posix()
        for file in (out / "snapshot").rglob("*")
        if file.is_file()
    }
    allowed = {"src/app.ts", "package.json", "src/feature/credentials.ts", "src/accounting.json"}
    assert allowed <= copied
    leaked = sorted((set(samples) - allowed) & copied)
    assert leaked == [], f"Private or generated paths entered the build context: {leaked}"
