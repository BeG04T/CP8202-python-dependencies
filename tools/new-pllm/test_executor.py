# Main orchestrator for the new-pllm dependency resolution pipeline.
# Coordinates LLM evaluation, Docker builds, error analysis, and iterative fixing.
import argparse
import json
import time
import multiprocessing as mp

from helpers.ollama_helper_tester import OllamaHelper
from helpers.py_pi_query import PyPIQuery
from helpers.build_dockerfile import DockerHelper
from helpers.local_helper import LocalHelper
from helpers.deps_scraper import DepsScraper


class TestExecutor:
    """Drives the full build → run → diagnose → fix loop for a Python snippet."""

    def __init__(self, base_url="http://localhost:11434", model="gemma2",
                 logging=True, temp=0.7, end_loop=5, search_range=1,
                 base_modules="./modules") -> None:
        print(f"Running model: {model} | temp: {temp} | loops: {end_loop} | range: {search_range}")
        self.ollama = OllamaHelper(
            base_url=base_url, model=model, logging=logging,
            temp=temp, base_modules=base_modules,
        )
        self.pypi = PyPIQuery(logging=True, base_modules=base_modules)
        self.deps = DepsScraper(logging=True)
        self.end_loop = end_loop
        self.search_range = search_range
        self.start_time = time.time()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def validate_json(text):
        try:
            json.loads(text)
            return True
        except ValueError:
            return False

    @staticmethod
    def read_python_file(filepath):
        with open(filepath, "r") as fh:
            return fh.read().replace("\n", "")

    def evaluate_file(self, llm, filepath):
        """First LLM pass – get modules and Python version."""
        result = llm.evaluate_file(filepath)
        result["python_version"] = str(result["python_version"])
        mods = result["python_modules"]
        if isinstance(mods, dict):
            result["python_modules"] = list(mods.keys())
        return result

    def resolve_modules(self, llm, llm_eval):
        """Use PyPI + LLM to pin specific versions for each module."""
        llm_eval["python_modules"], llm_eval["python_version"] = \
            llm.pypi.get_module_specifics(llm_eval)
        llm_eval["python_modules"] = llm.get_module_versions(llm_eval)
        return llm_eval

    def build_container(self, docker_helper, llm, llm_eval, filepath, error_info=None):
        """Build Docker image. Returns (success, docker_output, llm_output, error_type)."""
        if error_info is None:
            error_info = {}
        docker_helper.create_dockerfile(llm_eval, filepath)
        ok, build_output = docker_helper.build_dockerfile(filepath)
        if not ok:
            print(build_output)
            output, etype = llm.process_error(build_output, error_info, llm_eval)
            print("Docker build failed!")
            return False, build_output, output, etype
        print("Docker build complete!")
        return True, build_output, None, None

    # ------------------------------------------------------------------
    # Error tracking
    # ------------------------------------------------------------------
    def record_error(self, module, handler, error_type, llm_eval):
        """Track modules/versions that have caused errors."""
        handler[error_type] = handler.get(error_type, 0) + 1
        handler["previous"] = error_type
        if module and module.get("module") in llm_eval["python_modules"]:
            name = module["module"]
            cur_ver = llm_eval["python_modules"].get(name, "")
            handler.setdefault("error_modules", {})
            handler["error_modules"].setdefault(name, []).append(cur_ver)
        else:
            print("No previous this time!")
        return handler

    def apply_fix(self, new_module, llm_eval):
        """Apply the LLM's suggested fix to the module list."""
        updated = llm_eval.copy()
        updated["previous_python_modules"] = updated["python_modules"].copy()
        if new_module is None:
            return updated
        name = self.pypi.check_module_name(new_module["module"])
        name = name[0] if name else new_module["module"]
        ver = new_module.get("version")
        # Treat None, "None", empty, and whitespace-only as "remove this module"
        if ver is None or str(ver).strip().lower() in ("none", ""):
            updated["python_modules"].pop(name, None)
        else:
            updated["python_modules"][name] = str(ver).strip()
        return updated

    def reorder_modules(self, new_mod, move_mod, llm_eval):
        """Shuffle module install order to resolve ordering issues."""
        ordered = []
        mods = llm_eval["python_modules"].copy()
        for m in mods:
            if m == move_mod:
                if new_mod not in ordered:
                    ordered.append(new_mod)
                if move_mod not in ordered:
                    ordered.append(move_mod)
            else:
                if m not in ordered:
                    ordered.append(m)
        llm_eval["python_modules"] = {m: mods[m] for m in ordered if m in mods}
        return llm_eval

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_indent(line):
        if not line.startswith(" " * 8):
            return " " * 8 + line.lstrip()
        return line

    @staticmethod
    def _sanitise_line(line):
        line = line.replace("\t", "  ")
        if "TabError:" in line:
            line = "  " + line
        for ch in ("\u241b", "\u2408"):
            line = line.replace(ch, "")
        if "ETA" in line or "0us/step" in line:
            if not line.startswith(" " * 8):
                line = " " * 8 + line.lstrip()
        return line

    def write_iteration(self, log_path, llm_eval, docker_helper, etype, docker_msg,
                        iteration, finished):
        """Append one iteration's results to the YAML log file."""
        with open(log_path, "a") as fh:
            mods = llm_eval.get("previous_python_modules", llm_eval["python_modules"])
            fh.write(f"  iteration_{iteration}:\n")
            fh.write(f"    - python_module: {mods}\n")
            fh.write(f"    - error_type: {etype}\n")
            fh.write(f"    - error: |\n")
            if '"stream"' in docker_msg:
                docker_msg = docker_msg.replace('{"stream":"', "").replace(":", "")[:-5]
            prev_line = ""
            for line in docker_msg.split("\n"):
                if line:
                    extra = "  " if "^" in prev_line else ""
                    out = self._sanitise_line(f"        {line}\n")
                    fh.write(f"{extra}{out}")
                    prev_line = line
            print(iteration)
            if iteration + 1 > self.end_loop or finished:
                end_t = time.time()
                fh.write(f"end_time: {end_t}\n")
                fh.write(f"total_time: {end_t - self.start_time}")
        if iteration + 1 > self.end_loop or finished:
            docker_helper.delete_container()
            docker_helper.delete_image()
            exit(0)
        return iteration + 1

    # ------------------------------------------------------------------
    # Main process loop (runs per Python version)
    # ------------------------------------------------------------------
    def docker_process(self, llm, llm_eval, filepath, proc_id, no_docker=False):
        """Build → run → analyse → fix loop for one Python version."""
        docker_helper = LocalHelper(logging=True) if no_docker else DockerHelper(logging=True)
        llm_eval = self.resolve_modules(llm, llm_eval)
        print(llm_eval)

        error_handler = {
            "previous": "",
            "error_modules": {},
            "ImportError": 0, "ModuleNotFound": 0, "VersionNotFound": 0,
            "DependencyConflict": 0, "AttributeError": 0, "NonZeroCode": 0,
            "SyntaxError": 0,
        }
        # Per-module error counter: skip modules that fail too many times
        MAX_MODULE_ERRORS = 3
        module_error_counts = {}
        last_error_signature = None

        proj_dir, _, _ = docker_helper.get_project_dir(filepath)
        log_path = f"{proj_dir}/output_data_{llm_eval['python_version']}.yml"

        with open(log_path, "a") as fh:
            fh.write("---\n")
            fh.write(f"python_version: {llm_eval['python_version']}\n")
            fh.write(f"start_time: {self.start_time}\n")
            fh.write("iterations:\n")

        build_ok = False
        run_ok = False
        iteration = 1
        etype = "Unknown"
        docker_out = ""

        while not run_ok:
            try:
                print(f"In process {proc_id}")
                # Build loop
                while not build_ok:
                    build_ok, docker_out, output, etype = self.build_container(
                        docker_helper, llm, llm_eval, filepath, error_handler
                    )
                    if not build_ok:
                        # Detect repeated identical errors (stuck in a loop)
                        error_sig = f"{etype}:{output.get('module') if output else 'unknown'}"
                        if error_sig == last_error_signature:
                            mod_name = output.get("module") if output else None
                            if mod_name:
                                module_error_counts[mod_name] = module_error_counts.get(mod_name, 0) + 1
                                if module_error_counts[mod_name] >= MAX_MODULE_ERRORS:
                                    print(f"[SKIP] Module '{mod_name}' failed {MAX_MODULE_ERRORS} times, removing it")
                                    llm_eval["python_modules"].pop(mod_name, None)
                                    last_error_signature = None
                                    iteration = self.write_iteration(
                                        log_path, llm_eval, docker_helper, etype, docker_out, iteration, False
                                    )
                                    continue
                        last_error_signature = error_sig

                        error_handler = self.record_error(output, error_handler, etype, llm_eval)
                        llm_eval = self.apply_fix(output, llm_eval)
                        if etype == "ImportError" and "returned a non-zero code: 1" in docker_out:
                            zero_mod = llm.non_zero_error(docker_out)
                            llm_eval = self.reorder_modules(output["module"], zero_mod, llm_eval)
                        if etype == "NonZeroCode" and "PATH environment" in docker_out:
                            llm_eval["python_modules"].pop(output["module"], None)
                        iteration = self.write_iteration(
                            log_path, llm_eval, docker_helper, etype, docker_out, iteration, False
                        )

                # Run loop
                docker_out = docker_helper.run_container_test()
                print(docker_out)
                output, etype = llm.process_error(docker_out, error_handler, llm_eval)

                if etype == "ImportError":
                    if "DJANGO_SETTINGS_MODULE is undefined" in docker_out:
                        run_ok = True
                        llm_eval = self.apply_fix(output, llm_eval)
                    else:
                        build_ok = False
                        error_handler = self.record_error(output, error_handler, etype, llm_eval)
                        llm_eval = self.apply_fix(output, llm_eval)
                elif etype == "Py2SyntaxError":
                    # Python 2 code on Python 3 host — cannot be fixed via dependencies
                    print("[STOP] Python 2 syntax detected on Python 3 host — unresolvable in --no-docker mode")
                    run_ok = True
                elif etype in ("VersionNotFound", "DependencyConflict", "ModuleNotFound",
                               "AttributeError", "InvalidVersion", "SyntaxError"):
                    build_ok = False
                    error_handler = self.record_error(output, error_handler, etype, llm_eval)
                    llm_eval = self.apply_fix(output, llm_eval)
                elif etype == "NonZeroCode":
                    build_ok = False
                    error_handler = self.record_error(output, error_handler, etype, llm_eval)
                    if output and output.get("module") in llm_eval["python_modules"]:
                        llm_eval["python_modules"].pop(output["module"])
                elif etype == "NameError":
                    run_ok = True
                    error_handler = self.record_error(output, error_handler, etype, llm_eval)
                    llm_eval = self.apply_fix(output, llm_eval)
                elif etype == "None":
                    run_ok = True
                    llm_eval = self.apply_fix(None, llm_eval)
            except Exception as exc:
                print(f"Failed to build container: {exc}")

            iteration = self.write_iteration(
                log_path, llm_eval, docker_helper, etype, docker_out, iteration, run_ok
            )

        self.write_iteration(log_path, llm_eval, docker_helper, etype, docker_out,
                             self.end_loop, True)


# ======================================================================
# CLI entry point
# ======================================================================
def parse_args():
    def str2bool(v):
        if isinstance(v, bool):
            return v
        if v.lower() in ("yes", "true", "t", "y", "1"):
            return True
        if v.lower() in ("no", "false", "f", "n", "0"):
            return False
        raise argparse.ArgumentTypeError(f'Boolean value expected, got "{v}".')

    p = argparse.ArgumentParser(description="New-PLLM dependency resolver")
    p.add_argument("-f", "--file", type=str, help="Path to the Python snippet")
    p.add_argument("-b", "--base", type=str, nargs="?",
                   default="http://localhost:11434", const="http://localhost:11434",
                   help="Ollama API URL")
    p.add_argument("-m", "--model", type=str, nargs="?",
                   default="gemma2", const="gemma2", help="LLM model name")
    p.add_argument("-t", "--temp", type=str, nargs="?",
                   default="0.7", const="0.7", help="Model temperature (0-2)")
    p.add_argument("-l", "--loop", type=int, nargs="?",
                   default=5, const=5, help="Max iterations per version")
    p.add_argument("-r", "--range", type=int, nargs="?",
                   default=0, const=0, help="Python version search range")
    p.add_argument("-ra", "--rag", type=str2bool, nargs="?",
                   default=True, const=True, help="Enable RAG")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    p.add_argument("--no-docker", action="store_true",
                   help="Use local venv instead of Docker (no Docker required)")
    return p.parse_args()


def main():
    args = parse_args()
    file_dir = "/".join(args.file.split("/")[:-1])
    modules_dir = file_dir + "/modules"

    executor = TestExecutor(
        base_url=args.base, model=args.model, logging=True,
        temp=args.temp, end_loop=args.loop, search_range=args.range,
        base_modules=modules_dir,
    )

    # Simple import scraping (RAG stage)
    raw_deps = []
    if args.rag:
        raw_deps = executor.deps.find_word_in_file(args.file, "import", [])

    # LLM evaluation with retry
    llm_eval = None
    success = False
    retries = 0
    while not success and retries < 5:
        try:
            llm_eval = executor.evaluate_file(executor.ollama, args.file)
            combined = executor.pypi.check_module_name(raw_deps + llm_eval["python_modules"])
            llm_eval["python_modules"] = combined
            print(llm_eval)
            success = True
        except Exception as exc:
            print(f"Failed to get modules: {exc}")
            retries += 1

    if not success:
        llm_eval = {"python_version": "3.8"}
        llm_eval["python_modules"] = executor.pypi.check_module_name(raw_deps)

    # Determine Python versions to test
    versions = executor.pypi.get_python_range(
        python_version=llm_eval["python_version"], pyrange=executor.search_range
    )
    print(versions)
    if not versions:
        versions = executor.pypi.get_python_range(
            python_version=llm_eval["python_version"], pyrange=executor.search_range
        )

    num_procs = (executor.search_range * 2) + 1
    processes = []

    for i in range(num_procs):
        run_info = llm_eval.copy()
        run_info["python_version"] = versions[i]
        p = mp.Process(
            target=executor.docker_process,
            args=(
                OllamaHelper(
                    base_url=args.base, model=args.model, logging=True,
                    temp=args.temp, base_modules=modules_dir, rag=args.rag,
                ),
                run_info, args.file, i, args.no_docker,
            ),
        )
        processes.append(p)
        p.start()

    for p in processes:
        p.join(timeout=1200)

    for p in processes:
        if p.is_alive():
            p.terminate()
        else:
            print("Processing completed without timeout")


if __name__ == "__main__":
    main()
    print("Done")

