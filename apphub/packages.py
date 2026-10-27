"""Prepare ZIP projects and standalone HTML apps as Docker build contexts."""
import json
import fnmatch
from pathlib import Path, PurePosixPath
import stat
import shutil
import zipfile

MAX_UPLOAD = 100 * 1024 * 1024
MAX_EXPANDED = 500 * 1024 * 1024
IGNORED = {"__MACOSX", ".git", "node_modules", ".DS_Store", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".terraform", ".venv", "venv", "__pycache__"}
PRIVATE_FILES = (".env", ".env.*", ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.kdbx", "*.sqlite*", "*.db", "*.log", "*.bak", "*.dump")


def private_file(name):
    return any(fnmatch.fnmatch(name.lower(), pattern) for pattern in PRIVATE_FILES)


def unpack(archive: Path, destination: Path) -> Path:
    """Prepare an HTML entry page or safely extract regular ZIP files."""
    if archive.stat().st_size > MAX_UPLOAD:
        raise ValueError("Upload exceeds 100 MB.")
    destination.mkdir(parents=True, exist_ok=False)
    if archive.suffix.lower() in {".html", ".htm"}:
        with archive.open("rb") as source:
            if not any(chunk.strip() for chunk in iter(lambda: source.read(65536), b"")):
                raise ValueError("The HTML file is empty.")
        shutil.copyfile(archive, destination / "index.html")
        (destination / "index.html").chmod(0o644)
        return destination
    total = 0
    seen = set()
    try:
        with zipfile.ZipFile(archive) as z:
            if len(z.infolist()) > 10000:
                raise ValueError("ZIP has too many files (maximum 10,000).")
            for info in z.infolist():
                path = PurePosixPath(info.filename)
                if (not info.filename or "\\" in info.filename or "\x00" in info.filename
                        or path.is_absolute() or ".." in path.parts or ":" in info.filename):
                    raise ValueError("ZIP contains an unsafe file path.")
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("ZIP symlinks and special files are not supported.")
                if info.flag_bits & 1:
                    raise ValueError("Password-protected ZIPs are not supported.")
                total += info.file_size
                if total > MAX_EXPANDED:
                    raise ValueError("Expanded ZIP exceeds 500 MB.")
                if any(p in IGNORED for p in path.parts):
                    continue
                if private_file(path.name):
                    continue
                target = destination.joinpath(*path.parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("ZIP path leaves the project folder.")
                normalized = str(path)
                if normalized in seen:
                    raise ValueError("ZIP contains duplicate paths.")
                seen.add(normalized)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as source, target.open("xb") as out:
                        copied = 0
                        while chunk := source.read(65536):
                            copied += len(chunk)
                            if copied > info.file_size:
                                raise ValueError("Invalid ZIP file size.")
                            out.write(chunk)
                    target.chmod(0o644 | ((info.external_attr >> 16) & 0o111))
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("This is not a valid, readable ZIP file.") from exc
    root = destination
    while True:
        children = list(root.iterdir())
        if len(children) == 1 and children[0].is_dir():
            root = children[0]
        else:
            break
    if not list(root.iterdir()):
        raise ValueError("ZIP contains no application files.")
    # A static export may have a descriptive filename rather than index.html.
    # Keep that original URL as well as the entry-page alias. Never change
    # source-project detection for Node apps or custom Dockerfile builds.
    if not any((root / name).exists() for name in ("index.html", "package.json", "Dockerfile")):
        pages = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".html", ".htm"}]
        if len(pages) == 1:
            shutil.copyfile(pages[0], root / "index.html")
        elif len(pages) > 1:
            raise ValueError("ZIP contains multiple HTML pages. Name the app's starting page index.html.")
    return root


STATIC_SERVER = r'''import http.server, os, fnmatch
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(os.environ.get('SITE_ROOT', '/site')).resolve()
PRIVATE_FILES = __PRIVATE_FILES__
SOURCE_NAMES = {'dockerfile', 'compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml', 'apphub.json', 'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'requirements.txt', 'pyproject.toml'}
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)
    def send_head(self):
        path = unquote(urlsplit(self.path).path)
        if any(p.startswith('.') for p in Path(path).parts if p not in ('/', '.')):
            self.send_error(404)
            return None
        if Path(path).name.lower() in SOURCE_NAMES or any(fnmatch.fnmatch(Path(path).name.lower(), pattern) for pattern in PRIVATE_FILES):
            self.send_error(404)
            return None
        candidate = (ROOT / path.lstrip('/')).resolve()
        if not candidate.is_relative_to(ROOT):
            self.send_error(404)
            return None
        if not candidate.exists() and not Path(path).suffix and 'text/html' in self.headers.get('Accept', ''):
            self.path = '/index.html'
        return super().send_head()
    def list_directory(self, path):
        self.send_error(404)
        return None
    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()
http.server.ThreadingHTTPServer((os.environ.get('HOST', '0.0.0.0'), int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
'''.replace('__PRIVATE_FILES__', repr(PRIVATE_FILES))


def prepare(root: Path, requested_mode="auto", requested_port=None) -> dict:
    config_path = root / "apphub.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("apphub.json must contain an object.")
    mode = requested_mode if requested_mode != "auto" else config.get("type", "auto")
    if mode not in {"auto", "static", "node", "docker"}:
        raise ValueError("App type must be auto, static, node, or docker.")
    package = {}
    if (root / "package.json").exists():
        package = json.loads((root / "package.json").read_text())
        if not isinstance(package, dict):
            raise ValueError("package.json must contain an object.")
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    scripts = package.get("scripts", {})
    static_dir = config.get("static_dir")
    if static_dir is not None:
        if not isinstance(static_dir, str) or not static_dir or PurePosixPath(static_dir).is_absolute() or ".." in PurePosixPath(static_dir).parts or "\\" in static_dir:
            raise ValueError("static_dir must be a relative project directory.")
    built = next((d for d in (static_dir, "dist", "build", ".") if d and (root / d / "index.html").is_file()), None)
    if mode == "auto":
        if (root / "Dockerfile").is_file():
            mode = "docker"
        elif package and "build" in scripts and ("vite" in deps or "react-scripts" in deps):
            mode = "static"
        elif built and not (package and "next" in deps):
            mode = "static"
        elif package:
            mode = "node"
        else:
            raise ValueError("Could not identify this app. Upload HTML, a Node project, or a project with a Dockerfile.")
    port = requested_port or config.get("port") or (8080 if mode == "static" else 3000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Internal port must be between 1 and 65535.")
    health = config.get("health_path", "/")
    if not isinstance(health, str) or not health.startswith("/") or health.startswith("//") or any(c in health for c in "\r\n"):
        raise ValueError("health_path must be an HTTP path beginning with /.")
    # Dockerfiles are generated separately, without overwriting project sources.
    runtime = root / "__apphub_runtime"
    if runtime.exists():
        raise ValueError("The reserved folder __apphub_runtime already exists.")
    runtime.mkdir()
    if mode == "docker":
        if not (root / "Dockerfile").is_file():
            raise ValueError("Docker mode requires a Dockerfile at the project root.")
        dockerfile = "Dockerfile"
    elif mode == "static":
        (runtime / "serve.py").write_text(STATIC_SERVER)
        build_needed = package and "build" in scripts and (not built or "vite" in deps or "react-scripts" in deps)
        prefix = ""
        if build_needed:
            output = static_dir or ("build" if "react-scripts" in deps else "dist")
            if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-" for c in output):
                raise ValueError("Use a simple relative path for the build output directory.")
            prefix = "FROM node:22-alpine AS build\nWORKDIR /app\nCOPY . .\nRUN if [ -f package-lock.json ]; then npm ci; else npm install; fi\nRUN npm run build\n"
            copy = f"COPY --from=build /app/{output}/ /site/\n"
        elif built:
            # Only stage public assets; never copy hub helpers or project configs.
            source_names = {"dockerfile", "apphub.json", "package.json", "package-lock.json", "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "yarn.lock", "pnpm-lock.yaml", "requirements.txt", "pyproject.toml"}
            def exclude(directory, names):
                return [name for name in names if name.startswith(".") or name in IGNORED or name == "__apphub_runtime" or private_file(name) or name.lower() in source_names]
            shutil.copytree(root / built, runtime / "public", ignore=exclude)
            copy = 'COPY ["__apphub_runtime/public/", "/site/"]\n'
        else:
            raise ValueError("Static app needs index.html or a package.json build script.")
        docker = prefix + "FROM python:3.12-alpine\nWORKDIR /site\n" + copy + "COPY __apphub_runtime/serve.py /serve.py\nENV PORT=" + str(port) + "\nUSER 65534:65534\nEXPOSE " + str(port) + '\nCMD ["python", "-u", "/serve.py"]\n'
        (runtime / "Dockerfile").write_text(docker)
        dockerfile = "__apphub_runtime/Dockerfile"
    else:
        if "start" not in scripts:
            raise ValueError("Node app needs an npm start script. For a Vite app, select Static or Auto.")
        docker = "FROM node:22-alpine\nWORKDIR /app\nCOPY . .\nRUN if [ -f package-lock.json ]; then npm ci; else npm install; fi\n"
        if "build" in scripts:
            docker += "RUN npm run build\n"
        docker += f"ENV NODE_ENV=production PORT={port} HOST=0.0.0.0 HOSTNAME=0.0.0.0\nEXPOSE {port}\n" + 'CMD ["npm", "start"]\n'
        (runtime / "Dockerfile").write_text(docker)
        dockerfile = "__apphub_runtime/Dockerfile"
    with (root / ".dockerignore").open("a") as out:
        out.write("\n" + "\n".join([*sorted(IGNORED), *PRIVATE_FILES, *('**/' + p for p in PRIVATE_FILES), "*.zip", "!__apphub_runtime", "!__apphub_runtime/**"]) + "\n")
    return {"mode": mode, "container_port": port, "health_path": health, "dockerfile": dockerfile}
