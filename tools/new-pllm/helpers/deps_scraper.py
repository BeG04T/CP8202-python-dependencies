# Dependency scraper – extracts import statements from Python files
# and filters out standard library modules.
import os
import sys
import importlib.util
import sysconfig
import requests


class DepsScraper:
    """Scans Python source files for import statements and determines
    which imports correspond to third-party packages."""

    def __init__(self, logging=False) -> None:
        self.logging = logging

    # ------------------------------------------------------------------
    # Standard library detection
    # ------------------------------------------------------------------
    def is_module_in_standard_library(self, module_name):
        """Return True if *module_name* belongs to the Python standard library."""
        if module_name in sys.builtin_module_names:
            return True
        # Hard-coded overrides for ambiguous names
        if module_name in ("io", "stringio", "os"):
            return True
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            return False
        stdlib_path = sysconfig.get_paths()["stdlib"]
        return spec.origin.startswith(stdlib_path)

    def is_package_on_pypi(self, package_name):
        """Check whether *package_name* exists on PyPI and is not a stdlib module."""
        url = f"https://pypi.org/pypi/{package_name}/json"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            return not self.is_module_in_standard_library(package_name)
        except requests.HTTPError as err:
            if err.response.status_code == 404:
                return False
            raise

    # ------------------------------------------------------------------
    # File walking
    # ------------------------------------------------------------------
    def list_python_files(self, folder_path):
        """Walk *folder_path* and return (python_files, directories)."""
        dirs_found = []
        py_files = []
        for root, dirs, files in os.walk(folder_path):
            for d in dirs:
                dirs_found.append(d)
            for fname in files:
                if fname.endswith(".py") and not fname.endswith(".pyc"):
                    py_files.append(os.path.join(root, fname))
        return py_files, dirs_found

    # ------------------------------------------------------------------
    # Import helpers
    # ------------------------------------------------------------------
    def _handle_dot_notation(self, word, folders):
        """Strip sub-module paths and reject project-internal imports."""
        if "." in word:
            for folder in folders:
                if folder in word:
                    return None
            return word.split(".")[0]
        return word

    @staticmethod
    def _safe_append(lst, item):
        """Append *item* to *lst* only if not already present."""
        if item not in lst:
            lst.append(item)
        return lst

    @staticmethod
    def _toggle_block_quote(flag, line):
        if '"""' in line:
            return not flag
        return flag

    def clean_deps(self, dep_list):
        """Remove standard-library modules, title-cased names, and numeric-leading names."""
        cleaned = []
        for dep in dep_list:
            if not dep:
                continue
            if dep.istitle() or dep[0].isdigit():
                continue
            if not self.is_module_in_standard_library(dep):
                self._safe_append(cleaned, dep)
        return cleaned

    # ------------------------------------------------------------------
    # Main import extraction
    # ------------------------------------------------------------------
    def find_word_in_file(self, file_path, target_word, folders):
        """Scan *file_path* for lines containing *target_word* (typically 'import').
        Returns a list of module names found."""
        imports = []
        in_block_quote = False
        try:
            with open(file_path, "r") as fh:
                for line_no, line in enumerate(fh, start=1):
                    in_block_quote = self._toggle_block_quote(in_block_quote, line)
                    if in_block_quote:
                        continue
                    if target_word not in line:
                        continue
                    if self.logging:
                        print(f'Found "{target_word}" in {file_path} at line {line_no}:')
                    if "#" in line:
                        continue
                    tokens = line.strip().split(" ")
                    for idx, token in enumerate(tokens):
                        if idx == 0 and token == "import":
                            self._safe_append(imports, tokens[idx + 1])
                        elif idx > 0 and token == "import":
                            self._safe_append(imports, tokens[idx - 1])
        except FileNotFoundError:
            print(f"File not found: {file_path}")
        except Exception as exc:
            print(f"An error occurred: {exc}")
        return imports

