# Docker image builder – creates Dockerfiles, builds images, and runs containers.
import docker
from time import sleep
import os


class DockerHelper:
    """Manages the lifecycle of Docker images/containers for testing
    whether a Python snippet runs with the resolved dependencies."""

    def __init__(self, logging=False, image_name="", dockerfile_name="",
                 container_name="") -> None:
        self.dockerfile_content = ""
        self.image_name = image_name
        self.dockerfile_name = dockerfile_name
        self.container_name = container_name
        self.container = None
        self.logging = logging
        self.previous_error = {"error_message": "", "module": ""}

        try:
            self.client = docker.from_env()
        except docker.errors.DockerException as exc:
            if "Permission denied" in str(exc):
                print("\n" + "=" * 80)
                print("ERROR: Cannot access Docker socket - Permission Denied")
                print("=" * 80)
                print("\nEnsure Docker socket is mounted and permissions are correct.")
                print("=" * 80 + "\n")
            raise

    # ------------------------------------------------------------------
    # Path utilities
    # ------------------------------------------------------------------
    def get_project_dir(self, filepath):
        """Return (directory, dir_name, filename) from a full file path."""
        parts = filepath.split("/")
        directory = "/".join(parts[:-1])
        return directory, parts[-2], parts[-1]

    # ------------------------------------------------------------------
    # Dockerfile generation
    # ------------------------------------------------------------------
    def create_dockerfile(self, llm_out, filepath):
        """Write a Dockerfile that installs the resolved modules and copies the snippet."""
        proj_dir, dir_name, proj_file = self.get_project_dir(filepath)
        lines = [
            f"# Target Python version\n",
            f"FROM python:{llm_out['python_version']}\n",
            f"WORKDIR /app\n",
            f'RUN ["pip","install","--upgrade","pip"]\n',
        ]
        modules = llm_out["python_modules"]
        for mod in modules:
            if isinstance(mod, dict):
                name, ver = mod["module"], mod["version"]
            else:
                name, ver = mod, modules[mod]
            ver_str = ver if isinstance(ver, str) else ver[0]
            lines.append(
                f'RUN ["pip","install","--trusted-host","pypi.python.org",'
                f'"--default-timeout=100","{name}=={ver_str}"]\n'
            )
        lines.append(f"COPY {proj_file} /app\n")
        lines.append(f'CMD ["python", "/app/{proj_file}"]')

        self.dockerfile_content = "".join(lines)
        self.image_name = f"test/new-pllm:{dir_name}_{llm_out['python_version']}"
        self.container_name = f"{dir_name}_{llm_out['python_version']}"
        self.dockerfile_name = f"Dockerfile-llm-{llm_out['python_version']}"

        with open(f"{proj_dir}/{self.dockerfile_name}", "w") as fh:
            fh.write(self.dockerfile_content)

    # ------------------------------------------------------------------
    # Build / Run / Cleanup
    # ------------------------------------------------------------------
    def build_dockerfile(self, path, dockerfile=None):
        """Build the Docker image. Returns (success, error_output)."""
        if not dockerfile:
            dockerfile = self.dockerfile_name
        proj_dir, _, _ = self.get_project_dir(path)
        errors = ""
        for line in self.client.api.build(
            path=proj_dir, dockerfile=dockerfile, forcerm=True, tag=self.image_name
        ):
            decoded = line.decode("utf-8")
            if any(kw in decoded for kw in ("ERROR", "Could not fetch URL", "errorDetail")):
                errors += decoded
            if self.logging:
                print(decoded)
        return (True, "") if not errors else (False, errors)

    def delete_container(self):
        try:
            self.client.containers.get(self.container_name).remove(v=True, force=True)
        except Exception as exc:
            if self.logging:
                print(exc)

    def delete_image(self):
        try:
            self.client.images.remove(image=self.image_name, force=True)
        except Exception as exc:
            if self.logging:
                print(exc)

    def run_container_test(self):
        """Create, start, wait for, and collect logs from the test container."""
        self.delete_container()
        logs = ""
        try:
            self.container = self.client.containers.create(
                self.image_name, name=self.container_name
            )
            self.container.start()
            sleep(10)
            while self.container.status == "running":
                sleep(5)
            if self.logging:
                print(self.container.status)
            logs = self.container.logs()
            self.container.remove(v=True, force=True)
            self.container = None
        except docker.errors.ContainerError:
            if self.container:
                while self.container.status == "running":
                    sleep(5)
                logs = self.container.logs()
                self.container.remove(v=True, force=True)
                self.container = None
        return logs.decode("utf-8") if isinstance(logs, bytes) else logs


def main():
    dh = DockerHelper(logging=True)
    dh.run_container_test()


if __name__ == "__main__":
    main()

