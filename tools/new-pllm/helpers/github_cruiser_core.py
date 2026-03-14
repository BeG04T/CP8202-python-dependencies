# Core utilities for subprocess execution and JSON/API interactions
import subprocess
import json
import requests
import time


class GithubCruiserCore:
    """Provides helper methods for running shell commands, loading JSON data,
    and querying GitHub repository information."""

    def __init__(self, logging=False) -> None:
        self.logging = logging

    def call_subprocess(self, cmd=""):
        """Execute a shell command and return the completed process.
        Automatically retries when GitHub API rate limits are hit."""
        try:
            if self.logging:
                print(f"Executing: {cmd}")
            while True:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                if self.logging:
                    print(result.stdout)
                if "API rate limit exceeded" in result.stdout:
                    time.sleep(300)  # Wait 5 minutes on rate limit
                else:
                    return result
        except Exception as exc:
            print(exc)

    def file_exists(self, file_name):
        """Check whether a given filename matches known dependency file names."""
        dependency_files = [
            "requirements.txt",
            "Requirements.txt",
            "REQUIREMENTS.txt",
            "Pipfile",
            "setup.py",
            "Setup.py",
            "SETUP.py",
        ]
        found = file_name in dependency_files
        return found, file_name

    def find_files(self, file_list):
        """Scan a list of file metadata dicts for dependency files.
        Returns (found, directories, file_name)."""
        directories = []
        for entry in file_list:
            if self.logging:
                print(f"file: {entry['name']}")
            if "dir" in entry["type"]:
                directories.append(entry["name"])
            else:
                found, fname = self.file_exists(entry["name"])
                if found:
                    return True, directories, fname
        return False, directories, ""

    def call_process_convert_json(self, file_name, process_cmd):
        """Run a subprocess and parse its stdout as JSON."""
        try:
            result = self.call_subprocess(process_cmd)
        except Exception as exc:
            print(exc)
        return json.loads(result.stdout)

    def load_json_from_file(self, filepath):
        """Load and return JSON data from a local file."""
        with open(filepath) as fh:
            return json.load(fh)

    def get_repo_api_data(self, repo):
        """Fetch repository metadata from the GitHub API."""
        url = f"https://api.github.com/repos/{repo}"
        response = requests.get(url)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Error: {response.status_code}")
            print(response.text)
            return None

