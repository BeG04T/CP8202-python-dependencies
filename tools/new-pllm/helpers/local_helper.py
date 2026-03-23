# Local (venv-based) execution helper – mirrors DockerHelper's interface
# but uses subprocess + virtual environments instead of Docker.
import os
import subprocess
import shutil
import sys


class LocalHelper:
    """Manages the lifecycle of virtual environments for testing
    whether a Python snippet runs with the resolved dependencies.
    Drop-in replacement for DockerHelper when Docker is unavailable."""

    def __init__(self, logging=False, image_name="", dockerfile_name="",
                 container_name="") -> None:
        self.logging = logging
        self.image_name = image_name
        self.dockerfile_name = dockerfile_name
        self.container_name = container_name
        self.previous_error = {"error_message": "", "module": ""}

        # Internal state set by create_dockerfile
        self._modules = []       # list of (name, version) tuples
        self._snippet_path = ""  # absolute path to the snippet file
        self._proj_dir = ""
        self._proj_file = ""
        self._python_version = ""
        self._venv_dir = ""

    # ------------------------------------------------------------------
    # Path utilities (identical to DockerHelper)
    # ------------------------------------------------------------------
    def get_project_dir(self, filepath):
        """Return (directory, dir_name, filename) from a full file path."""
        parts = filepath.split("/")
        directory = "/".join(parts[:-1])
        return directory, parts[-2], parts[-1]

    # ------------------------------------------------------------------
    # Dependency setup (replaces Dockerfile generation)
    # ------------------------------------------------------------------
    def create_dockerfile(self, llm_out, filepath):
        """Store the dependency info – no actual Dockerfile is written.
        Kept as same method name for interface compatibility."""
        proj_dir, dir_name, proj_file = self.get_project_dir(filepath)

        self._modules = []
        modules = llm_out["python_modules"]
        for mod in modules:
            if isinstance(mod, dict):
                name, ver = mod["module"], mod["version"]
            else:
                name, ver = mod, modules[mod]
            ver_str = ver if isinstance(ver, str) else ver[0]
            ver_str = ver_str.strip() if ver_str else ""
            self._modules.append((name, ver_str))

        self._snippet_path = filepath
        self._proj_dir = proj_dir
        self._proj_file = proj_file
        self._python_version = llm_out["python_version"]
        self._venv_dir = os.path.join(proj_dir, f".venv_{dir_name}_{self._python_version}")

        self.image_name = f"local:{dir_name}_{self._python_version}"
        self.container_name = f"{dir_name}_{self._python_version}"
        self.dockerfile_name = f"local-deps-{self._python_version}"

    # ------------------------------------------------------------------
    # Build (create venv + pip install)
    # ------------------------------------------------------------------
    def _create_venv(self):
        """Create a fresh virtual environment."""
        if os.path.exists(self._venv_dir):
            shutil.rmtree(self._venv_dir)
        subprocess.run(
            [sys.executable, "-m", "venv", self._venv_dir],
            capture_output=True, text=True, timeout=60,
        )

    def _venv_python(self):
        """Return the path to the venv's python binary."""
        return os.path.join(self._venv_dir, "bin", "python")

    def _venv_pip(self):
        """Return the path to the venv's pip binary."""
        return os.path.join(self._venv_dir, "bin", "pip")

    def build_dockerfile(self, path, dockerfile=None):
        """Create venv and pip install each dependency. Returns (success, error_output)."""
        self._create_venv()

        # Upgrade pip first
        subprocess.run(
            [self._venv_pip(), "install", "--upgrade", "pip"],
            capture_output=True, text=True, timeout=120,
        )

        errors = ""
        for name, ver in self._modules:
            # Skip modules with empty/invalid versions
            if not ver or ver.lower() in ("none",):
                if self.logging:
                    print(f"[local] SKIP: {name} (no valid version)")
                continue
            pkg = f"{name}=={ver}"
            if self.logging:
                print(f"[local] pip install {pkg}")
            result = subprocess.run(
                [self._venv_pip(), "install", "--default-timeout=100", pkg],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                err_text = result.stderr + result.stdout
                errors += err_text
                if self.logging:
                    print(f"[local] FAILED: {pkg}\n{err_text[:500]}")

        return (True, "") if not errors else (False, errors)

    # ------------------------------------------------------------------
    # Run snippet
    # ------------------------------------------------------------------
    def run_container_test(self):
        """Run the snippet inside the venv and return combined output."""
        try:
            result = subprocess.run(
                [self._venv_python(), self._snippet_path],
                capture_output=True, text=True, timeout=60,
            )
            output = result.stdout + result.stderr
            if self.logging:
                print(f"[local] snippet exit code: {result.returncode}")
            return output
        except subprocess.TimeoutExpired:
            return "TimeoutError: snippet execution exceeded 60 seconds"
        except Exception as exc:
            return f"ExecutionError: {exc}"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def delete_container(self):
        """Remove the virtual environment directory."""
        if self._venv_dir and os.path.exists(self._venv_dir):
            try:
                shutil.rmtree(self._venv_dir)
                if self.logging:
                    print(f"[local] Removed venv: {self._venv_dir}")
            except Exception as exc:
                if self.logging:
                    print(f"[local] Failed to remove venv: {exc}")

    def delete_image(self):
        """No-op for local mode (no image to delete)."""
        pass

