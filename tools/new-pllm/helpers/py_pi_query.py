# PyPI version querying – resolves module versions compatible with a target Python version.
import json
import os
import re
from pypi_json import PyPIJSON
from datetime import datetime

from helpers.github_cruiser_core import GithubCruiserCore
from helpers.deps_scraper import DepsScraper


class PyPIQuery:
    """Queries PyPI for module release information and filters versions
    based on the target Python version's release window."""

    def __init__(self, logging=False, base_modules="./modules") -> None:
        self.date_fmt = "%Y-%m-%d"
        self.out_date_fmt = "%b %d %Y"
        self.logging = logging
        self.ghc = GithubCruiserCore(logging=False)
        self.deps = DepsScraper(logging=logging)
        self.python_versions = self.ghc.load_json_from_file(
            "helpers/ref_files/python_versions.json"
        )
        os.makedirs(base_modules, exist_ok=True)
        self.base_modules = base_modules

    # ------------------------------------------------------------------
    # Python version utilities
    # ------------------------------------------------------------------
    def check_format(self, python_version):
        """Normalise a Python version string to 'major.minor' form."""
        cleaned = python_version.replace("+", "")
        parts = cleaned.split(".")
        if len(parts) == 1:
            return f"{parts[0]}.7"
        minor = parts[1] if parts[1] != "x" else "7"
        return f"{parts[0]}.{minor}"

    def get_python_dates(self, python_version):
        """Return (release_date, next_release_date, normalised_version) for a Python version."""
        ver = self.check_format(python_version)
        for idx, entry in enumerate(self.python_versions):
            if ver in entry["cycle"]:
                if idx > 0:
                    next_date = datetime.strptime(
                        self.python_versions[idx - 1]["releaseDate"], self.date_fmt
                    ).date()
                else:
                    next_date = datetime.now().strftime(self.date_fmt)
                release_date = datetime.strptime(entry["releaseDate"], self.date_fmt).date()
                return release_date, next_date, ver
        return None, None, ver

    def get_python_range(self, python_version, pyrange=2):
        """Return a list of Python version strings centred on *python_version*
        spanning *pyrange* versions either side."""
        ver = self.check_format(python_version)
        selected = []
        try:
            for idx, entry in enumerate(self.python_versions):
                if ver in entry["cycle"]:
                    total = 1 + (pyrange * 2)
                    start = max(0, idx - pyrange)
                    end = min(len(self.python_versions), idx + pyrange + 1)
                    if end - start < total:
                        if start == 0:
                            end += total - (end - start)
                        else:
                            start -= total - (end - start)
                    for v in self.python_versions[start:end]:
                        selected.append(v["cycle"])
        except Exception as exc:
            print(f"Unable to get Python version: {exc}")

        if not selected:
            for i in range(pyrange + 1):
                if i == 0:
                    selected.append("3.8")
                elif i == 1:
                    selected.append("2.7")
                    selected.append(f"3.{8 + i}")
                else:
                    selected.append(f"3.{8 + i}")
                    selected.append(f"3.{8 - i}")
        elif "2.7" not in selected:
            if selected:
                selected[-1] = "2.7"
            else:
                selected.append("2.7")

        return selected

    # ------------------------------------------------------------------
    # Module file I/O
    # ------------------------------------------------------------------
    def read_module_file(self, module, python_version):
        """Read the cached version list for *module* at *python_version*."""
        path = f"{self.base_modules}/{module}_{python_version}.txt"
        if os.path.isfile(path):
            with open(path, "r") as fh:
                return fh.read()
        # Generate cache on-the-fly
        self.get_module_specifics({"python_version": python_version, "python_modules": [module]})
        if os.path.isfile(path):
            with open(path, "r") as fh:
                return fh.read()
        return ""

    # ------------------------------------------------------------------
    # Module name resolution
    # ------------------------------------------------------------------
    def _load_module_links(self):
        with open("./helpers/ref_files/module_link.json") as fh:
            return json.load(fh)

    def check_modules(self, modules):
        """Map module dict keys through the known-aliases file."""
        known = self._load_module_links()
        result = {}
        for mod in modules:
            name = mod.split(".")[0] if "." in mod else mod
            version = modules[mod]
            if name in known:
                result[known[name.lower()]["ref"]] = version
            else:
                result[name.lower()] = version
        return result

    def check_module_name(self, module_name):
        """Resolve a list (or single string) of module names through known aliases,
        then filter out stdlib modules."""
        known = self._load_module_links()
        resolved = []
        if isinstance(module_name, str):
            module_name = [module_name]
        for mod in module_name:
            name = mod.split(".")[0] if "." in mod else mod
            name = name.replace(";", "").replace(",", "")
            if name.lower() in known:
                resolved.append(known[name.lower()]["ref"])
            else:
                resolved.append(name.lower())
        return self.deps.clean_deps(resolved)

    # ------------------------------------------------------------------
    # PyPI queries
    # ------------------------------------------------------------------
    def query_module(self, module_name):
        """Fetch full release metadata for *module_name* from PyPI."""
        try:
            with PyPIJSON() as client:
                return client.get_metadata(module_name)
        except Exception:
            return None

    def find_modules(self, module_name, start_date, end_date, python_version):
        """Return a list of {'version', 'date'} dicts for *module_name*
        whose uploads fall within the target date window."""
        meta = self.query_module(module_name)
        if not meta:
            return []

        releases = meta.releases
        latest = {"version": "", "date": datetime.strptime("1981-10-02", self.date_fmt).date()}
        small_repo = len(releases) <= 5
        collected = []

        for ver_str, rel_list in releases.items():
            if not rel_list:
                continue
            stored_one = False
            for detail in rel_list:
                if stored_one:
                    break
                if detail["yanked"]:
                    continue
                upload = datetime.strptime(
                    detail["upload_time"].split("T")[0], "%Y-%m-%d"
                ).date()
                entry = None

                if small_repo:
                    entry = {"version": ver_str, "date": upload.strftime(self.out_date_fmt)}
                if upload >= start_date and upload <= end_date:
                    entry = {"version": ver_str, "date": upload.strftime(self.out_date_fmt)}
                elif self._extract_python_version(detail["python_version"]) == python_version:
                    entry = {"version": ver_str, "date": upload.strftime(self.out_date_fmt)}
                elif "py2" in detail["python_version"] and "2." in python_version:
                    entry = {"version": ver_str, "date": upload.strftime(self.out_date_fmt)}
                elif "py3" in detail["python_version"] and "3." in python_version:
                    entry = {"version": ver_str, "date": upload.strftime(self.out_date_fmt)}
                elif "source" in detail["python_version"] and len(collected) <= 20:
                    entry = {"version": ver_str, "date": upload.strftime(self.out_date_fmt)}

                if upload >= latest["date"]:
                    latest = {"version": ver_str, "date": upload}

                if entry:
                    collected.append(entry)
                    stored_one = True

        if not collected:
            latest["date"] = latest["date"].strftime(self.out_date_fmt)
            collected.append(latest)
        return collected

    # ------------------------------------------------------------------
    # Module specifics
    # ------------------------------------------------------------------
    def get_module_specifics(self, module_details=None):
        """For each module in *module_details*, query PyPI, filter versions,
        and write a cached text file. Returns (module_list, python_version)."""
        if module_details is None:
            module_details = {}
        start_date, end_date, python_version = self.get_python_dates(
            module_details["python_version"]
        )
        modules = self.check_module_name(module_details["python_modules"])
        final_modules = []

        def _version_key(v):
            return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v)]

        for dep in modules:
            found = self.find_modules(dep, start_date, end_date, python_version)
            versions = sorted([m["version"] for m in found], key=_version_key)
            final_modules.append(dep)
            with open(f"{self.base_modules}/{dep}_{python_version}.txt", "w") as out:
                out.write(", ".join(versions))

        return final_modules, python_version

    def _extract_python_version(self, code):
        """Convert a classifier like 'cp37' to '3.7'."""
        if "cp" not in code:
            return code
        digits = code[2:]
        return ".".join(digits)


def main():
    pq = PyPIQuery()
    details = {"python_version": "2.7", "python_modules": ["clipboard"]}
    pq.get_module_specifics(details)


if __name__ == "__main__":
    main()

