# LLM-driven dependency resolution and error analysis.
# Prompts the model to extract modules, infer versions, and diagnose build errors.
import argparse
import re

from helpers.ollama_helper_base import OllamaHelperBase
from helpers.py_pi_query import PyPIQuery

from langchain_core.pydantic_v1 import BaseModel, Field
from typing import Dict, List
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate


# ── Pydantic schemas for structured LLM output ──────────────────────
class Module(BaseModel):
    module: str = Field(description="Name of the module")


class ModuleVersion(BaseModel):
    module: str = Field(description="Name of the module")
    version: str = Field(description="The version of the module to use")


class PythonFile(BaseModel):
    python_version: str = Field(description="Python version required for this file")
    python_modules: List[str]


# ── Regex-based error parsing (reduces LLM hallucination) ────────────
class ErrorParser:
    """Extract structured information from Python/Docker error messages
    using regex before falling back to the LLM. This dramatically reduces
    hallucination because the LLM only needs to confirm, not guess."""

    # Pattern: ModuleNotFoundError: No module named 'xxx'
    _MODULE_NOT_FOUND = re.compile(
        r"ModuleNotFoundError:\s*No module named\s+['\"]([a-zA-Z0-9_]+)['\"]"
    )
    # Pattern: ImportError: cannot import name 'yyy' from 'xxx'
    _IMPORT_FROM = re.compile(
        r"ImportError:.*cannot import name\s+['\"](\w+)['\"]\s+from\s+['\"]([a-zA-Z0-9_.]+)['\"]"
    )
    # Pattern: ImportError: No module named xxx
    _IMPORT_NO_MODULE = re.compile(
        r"ImportError:\s*No module named\s+['\"]?([a-zA-Z0-9_]+)['\"]?"
    )
    # Pattern: from module_name==version (pip errors)
    _PIP_MODULE_VERSION = re.compile(
        r"([a-zA-Z0-9_-]+)==([0-9]+(?:\.[0-9]+)*[a-zA-Z0-9]*)"
    )
    # Pattern: Could not find a version that satisfies the requirement xxx
    _VERSION_REQUIREMENT = re.compile(
        r"Could not find a version that satisfies the requirement\s+([a-zA-Z0-9_-]+)"
    )
    # Pattern: AttributeError: module 'xxx' has no attribute 'yyy'
    _ATTRIBUTE_ERROR = re.compile(
        r"AttributeError:\s*module\s+['\"]([a-zA-Z0-9_.]+)['\"]\s+has no attribute"
    )
    # Pattern: non-zero code with module==version
    _NON_ZERO_MODULE = re.compile(
        r"(?:pip install|RUN pip).*?([a-zA-Z0-9_-]+)==[0-9].*?(?:non-zero|error|failed)"
    , re.IGNORECASE | re.DOTALL)

    @classmethod
    def extract_module_from_error(cls, error_message, error_type):
        """Try to extract the module name from an error message using regex.
        Returns the module name string or None if regex can't determine it."""
        if error_type == "ModuleNotFound":
            match = cls._MODULE_NOT_FOUND.search(error_message)
            if match:
                return match.group(1).split(".")[0]
        elif error_type == "ImportError":
            match = cls._IMPORT_FROM.search(error_message)
            if match:
                return match.group(2).split(".")[0]
            match = cls._IMPORT_NO_MODULE.search(error_message)
            if match:
                return match.group(1).split(".")[0]
        elif error_type == "VersionNotFound":
            match = cls._VERSION_REQUIREMENT.search(error_message)
            if match:
                return match.group(1)
            # Fallback: look for module==version pattern
            match = cls._PIP_MODULE_VERSION.search(error_message)
            if match:
                return match.group(1)
        elif error_type == "AttributeError":
            match = cls._ATTRIBUTE_ERROR.search(error_message)
            if match:
                return match.group(1).split(".")[0]
        elif error_type == "NonZeroCode":
            # Search for the last pip install that failed
            all_pip = cls._PIP_MODULE_VERSION.findall(error_message)
            if all_pip:
                return all_pip[-1][0]
        return None

    @classmethod
    def extract_version_from_error(cls, error_message):
        """Try to extract a version number from the error message."""
        match = cls._PIP_MODULE_VERSION.search(error_message)
        if match:
            return match.group(2)
        return None


# ── Main helper ─────────────────────────────────────────────────────
class OllamaHelper(OllamaHelperBase):
    """Wraps LLM calls for each stage of the dependency-resolution pipeline."""

    def __init__(self, base_url="http://localhost:11434", model="llama3",
                 temp=1.0, logging=False, base_modules="./modules", rag=True) -> None:
        super().__init__(base_url, model, temp, logging)
        self.base_modules = base_modules
        self.rag = rag
        self.pypi = PyPIQuery(logging=logging, base_modules=base_modules)

    # ── Validation helpers ───────────────────────────────────────────
    def pydantic_validate(self, schema, data):
        try:
            schema.parse_obj(data)
            return True
        except Exception:
            return False

    @staticmethod
    def is_valid_version(version):
        pattern = r"^\d+(\.\d+){1,2}([a-zA-Z0-9]+)?$"
        return bool(re.match(pattern, version))

    # ── Stage 1: initial file evaluation ─────────────────────────────
    def evaluate_file(self, python_file):
        """Ask the LLM to identify modules and Python version from a source file."""
        raw = self.read_python_file(python_file)
        parser = JsonOutputParser(pydantic_object=PythonFile)
        prompt = PromptTemplate(
            template=(
                "Given a python file:{raw_file}\n"
                "Return just a list of Python modules and python version required to run. "
                "Output JSON based on the schema {format_instructions}"
            ),
            input_variables=[],
            partial_variables={
                "raw_file": raw,
                "format_instructions": parser.get_format_instructions(),
            },
        )
        chain = prompt | self.model | parser
        result = chain.invoke({})
        print(result)
        return result

    # ── Stage 2: get specific versions for each module ───────────────
    def get_module_specifics(self, llm_eval):
        llm_eval["python_modules"], llm_eval["python_version"] = \
            self.pypi.get_module_specifics(llm_eval)
        llm_eval["python_modules"] = self.get_module_versions(llm_eval)
        return llm_eval

    def get_module_versions(self, details):
        """For every module, pick a version — algorithmically first, LLM as fallback."""
        modules = details["python_modules"]
        if not modules:
            return {}

        updated = {}
        for mod in modules:
            # Primary: algorithmic selection (pick latest available)
            algo_pick = self.pypi.select_version_algorithmically(
                mod, details["python_version"]
            )
            if algo_pick:
                updated[mod] = algo_pick
                print(f"[ALGO] {mod} -> {algo_pick}")
                continue

            # Fallback: LLM selection
            parser = JsonOutputParser(pydantic_object=ModuleVersion)
            versions_text = self.read_python_file(
                f"{self.base_modules}/{mod}_{details['python_version']}.txt"
            )
            attempts = 3
            while attempts > 0:
                try:
                    if self.rag and versions_text:
                        tpl = (
                            "Given a comma separated list of '{version_details}', "
                            "for the '{module}' module, from oldest to newest.\n"
                            "Select the most recent stable version. "
                            "Return the information with the format {format_instructions}"
                        )
                        pvars = {
                            "version_details": versions_text,
                            "module": mod,
                            "format_instructions": parser.get_format_instructions(),
                        }
                    else:
                        tpl = (
                            "Infer a possible working version of the '{module}' module "
                            "for Python {python_version}.\nReturn the information with "
                            "the format {format_instructions}"
                        )
                        pvars = {
                            "module": mod,
                            "python_version": details["python_version"],
                            "format_instructions": parser.get_format_instructions(),
                        }
                    prompt = PromptTemplate(
                        template=tpl, input_variables=[], partial_variables=pvars
                    )
                    chain = prompt | self.model | parser
                    out = chain.invoke({})
                    version = out["version"].split(" ")[0]
                    # Validate against cached list
                    if self.pypi.validate_version(mod, version, details["python_version"]):
                        updated[out["module"]] = version
                        print(f"[LLM-validated] {mod} -> {version}")
                    else:
                        # LLM picked invalid version — find closest real one
                        closest = self.pypi.find_closest_version(
                            mod, version, details["python_version"]
                        )
                        if closest:
                            updated[mod] = closest
                            print(f"[LLM-corrected] {mod} -> {closest} (LLM said {version})")
                        else:
                            updated[out["module"]] = version
                            print(f"[LLM-unvalidated] {mod} -> {version}")
                    break
                except Exception:
                    attempts -= 1

            if mod not in updated:
                print(f"[WARN] Failed to find version for {mod}")

        print(updated)
        return updated

    # ── Deprecated chain executor (kept for compatibility) ───────────
    def execute_chain(self, chain, pydantic_model):
        loop = 5
        while loop > 0:
            out = chain.invoke({})
            if self.logging:
                print(out)
            if self.pydantic_validate(pydantic_model, out):
                return True, out
            loop -= 1
        return False, None

    # ── Generic error helpers ────────────────────────────────────────
    def _get_module_from_error(self, prompt, parser, error_message=None, error_type=None):
        """Extract module name: try regex first, then fall back to LLM."""
        # Primary: regex extraction (fast, deterministic, no hallucination)
        if error_message and error_type:
            regex_module = ErrorParser.extract_module_from_error(error_message, error_type)
            if regex_module:
                resolved = self.pypi.check_module_name(regex_module)[0]
                if resolved:
                    print(f"[REGEX] Extracted module: {resolved} (from {error_type})")
                    return resolved

        # Fallback: LLM extraction
        for _ in range(5):
            try:
                chain = prompt | self.model | parser
                out = chain.invoke({})
                bad = self.pypi.check_module_name(out["module"])[0]
                if bad:
                    print(f"[LLM] Extracted module: {bad}")
                    return bad
            except Exception as exc:
                print(f"Error getting module name from error: {exc}")
        return None

    def _get_version_excluding_previous(self, prompt, parser, prev_versions,
                                         module=None, details=None):
        """Pick a version: try algorithmic selection first, then LLM fallback."""
        prev_set = set(v.strip() for v in prev_versions.split(",") if v.strip())

        # Primary: algorithmic version selection
        if module and details:
            algo_pick = self.pypi.select_version_algorithmically(
                module, details.get("python_version", ""), excluded=prev_set
            )
            if algo_pick:
                print(f"[ALGO] Version for {module}: {algo_pick}")
                return {"module": module, "version": algo_pick}

        # Fallback: LLM selection with validation
        out = None
        for _ in range(5):
            try:
                chain = prompt | self.model | parser
                out = chain.invoke({})
                print(out)
                version = out.get("version")
                if version and version in prev_set:
                    out = None
                    continue
                if out and (version is None or self.is_valid_version(version)):
                    # Validate against cached list if possible
                    if module and details and version:
                        if not self.pypi.validate_version(module, version, details.get("python_version", "")):
                            closest = self.pypi.find_closest_version(
                                module, version, details.get("python_version", ""), excluded=prev_set
                            )
                            if closest:
                                out["version"] = closest
                                print(f"[LLM-corrected] {module}: {version} -> {closest}")
                    return out
            except Exception as exc:
                print(f"Error getting versions from error: {exc}")
        if out:
            for v in prev_set:
                if v == out.get("version"):
                    out["version"] = None
        return out

    def _fetch_versions_and_history(self, module, prev_info, details):
        """Load cached versions and compile previous error versions for a module."""
        versions = self.pypi.read_module_file(module, details["python_version"])
        prev = ""
        if module in prev_info["error_modules"]:
            prev = ", ".join(prev_info["error_modules"][module])
        if module in details["python_modules"]:
            cur = details["python_modules"][module]
            prev += cur if not prev else f", {cur}"
        return versions, prev

    # ── Error-specific handlers ──────────────────────────────────────
    def could_not_find_version(self, error, prev_info, details):
        parser = JsonOutputParser(pydantic_object=Module)
        prompt = PromptTemplate(
            template=(
                "Given a docker build error where a version could not be found:\n{error}\n"
                "Identify the module causing the error, which is likely in the form "
                "'from module_name==version'.\nReturn just the name of the module using "
                "the format instructions.\n{format_instructions}"
            ),
            input_variables=[],
            partial_variables={"error": error, "format_instructions": parser.get_format_instructions()},
        )
        bad = self._get_module_from_error(prompt, parser, error_message=error, error_type="VersionNotFound")
        if bad is None:
            return None

        versions, prev_str = self._fetch_versions_and_history(bad, prev_info, details)
        parser = JsonOutputParser(pydantic_object=ModuleVersion)

        if self.rag:
            tpl = (
                "Given a could not find a version error for the '{module}' module:\n{error}\n"
                "Excluding previous versions: ({previous_versions}), perform a distributed search "
                "over the recommended versions in the error message!\n"
                "Return the information with the format {format_instructions}, "
                "use None for version if no version could be found!"
            )
            pvars = {"module": bad, "error": error, "previous_versions": prev_str,
                      "format_instructions": parser.get_format_instructions()}
        else:
            tpl = (
                "Given a could not find a version error for the '{module}' module:\n{error}\n"
                "Infer a possible working version for Python {python_version}.\n"
                "Return the information with the format {format_instructions}, "
                "use None for version if no version could be found!"
            )
            pvars = {"module": bad, "error": error, "python_version": details["python_version"],
                      "format_instructions": parser.get_format_instructions()}

        ver_prompt = PromptTemplate(template=tpl, input_variables=[], partial_variables=pvars)
        out = self._get_version_excluding_previous(ver_prompt, parser, prev_str,
                                                    module=bad, details=details)

        if out and out.get("module") != bad:
            # Retry with version list
            if self.rag:
                tpl2 = (
                    "Excluding previous versions: ({previous_versions}). Perform a distributed "
                    "search over the '{module}' module versions: {versions}, selecting one to install.\n"
                    "Return the information with the format {format_instructions}"
                )
                pvars2 = {"versions": versions, "module": bad, "previous_versions": prev_str,
                           "format_instructions": parser.get_format_instructions()}
            else:
                tpl2 = (
                    "Infer a possible working version of the '{module}' module for Python "
                    "{python_version}.\nReturn the information with the format {format_instructions}"
                )
                pvars2 = {"module": bad, "python_version": details["python_version"],
                           "format_instructions": parser.get_format_instructions()}
            ver_prompt2 = PromptTemplate(template=tpl2, input_variables=[], partial_variables=pvars2)
            out = self._get_version_excluding_previous(ver_prompt2, parser, prev_str,
                                                        module=bad, details=details)

        return out if out and "module" in out and "version" in out else None

    def dependency_conflict(self, error):
        parser = JsonOutputParser(pydantic_object=ModuleVersion)
        prompt = PromptTemplate(
            template=(
                "Given a dependency conflict error:\n{error}\nReturn the module and a working "
                "version that would fix the error using the format {format_instructions}"
            ),
            input_variables=[],
            partial_variables={"error": error, "format_instructions": parser.get_format_instructions()},
        )
        chain = prompt | self.model | parser
        _, result = self.execute_chain(chain, ModuleVersion)
        print(result)
        return result

    def _version_from_error_generic(self, error, prev_info, details, error_prompt_tpl,
                                     error_type=None):
        """Shared logic for import_error, module_not_found, attribute_error, syntax_error."""
        parser = JsonOutputParser(pydantic_object=Module)
        mod_prompt = PromptTemplate(
            template=error_prompt_tpl,
            input_variables=[],
            partial_variables={"error": error, "format_instructions": parser.get_format_instructions(),
                                "python_modules": ", ".join(list(details.get("python_modules", {}).keys()))},
        )
        bad = self._get_module_from_error(mod_prompt, parser,
                                           error_message=error, error_type=error_type)
        if bad is None:
            return None

        versions, prev_str = self._fetch_versions_and_history(bad, prev_info, details)
        parser = JsonOutputParser(pydantic_object=ModuleVersion)

        if self.rag:
            tpl = (
                "Given a comma separated list of 'Module versions' for the '{module}' module, "
                "from oldest to newest:\n{module_versions}\n"
                "Perform equally distanced sampling to return a version from the given versions, "
                "excluding previously used versions ({previous_versions}).\n"
                "Return the information with the format {format_instructions}"
            )
            pvars = {"error": error, "module": bad, "module_versions": versions,
                      "previous_versions": prev_str, "format_instructions": parser.get_format_instructions()}
        else:
            tpl = (
                "Infer a possible working version of the '{module}' module for Python "
                "{python_version}.\nReturn the information with the format {format_instructions}"
            )
            pvars = {"error": error, "module": bad, "python_version": details["python_version"],
                      "format_instructions": parser.get_format_instructions()}

        ver_prompt = PromptTemplate(template=tpl, input_variables=[], partial_variables=pvars)
        out = self._get_version_excluding_previous(ver_prompt, parser, prev_str,
                                                    module=bad, details=details)
        return out if out and "module" in out and "version" in out else None

    def import_error(self, error, prev_info, details):
        tpl = (
            "Given an ImportError:\n{error}\n Identify the import which is causing the error.\n"
            "For this type of error, the module is normally in the text 'from x import y', "
            "where x and y are the module to import and the offending method.\n"
            "Return the name of the module using the format instructions.\n{format_instructions}"
        )
        return self._version_from_error_generic(error, prev_info, details, tpl,
                                                 error_type="ImportError")

    def module_not_found(self, error, prev_info, details):
        tpl = (
            "Given a ModuleNotFound:\n{error}\nIdentify the module being imported which is "
            "causing this error.\nReturn the name of the module using the format instructions.\n"
            "{format_instructions}"
        )
        return self._version_from_error_generic(error, prev_info, details, tpl,
                                                 error_type="ModuleNotFound")

    def attribute_error(self, error, prev_info, details):
        tpl = (
            "Given an AttributeError:\n{error}\n Use your knowledge of Python to identify "
            "which of the existing modules ({python_modules}) is causing the error.\n"
            "Return the name of the module using the format instructions.\n{format_instructions}"
        )
        return self._version_from_error_generic(error, prev_info, details, tpl,
                                                 error_type="AttributeError")

    def syntax_error_helper(self, error, prev_info, details):
        tpl = (
            "Given a Docker build error message: {error}\nIdentify the offending Python module "
            "and output the module name using the following format instruction {format_instructions}."
        )
        return self._version_from_error_generic(error, prev_info, details, tpl)

    def invalid_version(self, error):
        parser = JsonOutputParser(pydantic_object=ModuleVersion)
        prompt = PromptTemplate(
            template=(
                "Given a docker invalid versions error\n{error}\nReturn the Python module "
                "(not pip) and a working version that would fix the error using the format "
                "{format_instructions}"
            ),
            input_variables=[],
            partial_variables={"error": error, "format_instructions": parser.get_format_instructions()},
        )
        chain = prompt | self.model | parser
        _, result = self.execute_chain(chain, ModuleVersion)
        print(result)
        return result

    def non_zero_error(self, error):
        parser = JsonOutputParser(pydantic_object=Module)
        prompt = PromptTemplate(
            template=(
                "Given a docker build non-zero error:\n{error}\n Identify the module which "
                "failed to install with pip, this will typically be in the form module==version, "
                "where module is the module we want.\nReturn the name of the module using the "
                "format instructions.\n{format_instructions}"
            ),
            input_variables=[],
            partial_variables={"error": error, "format_instructions": parser.get_format_instructions()},
        )
        return self._get_module_from_error(prompt, parser,
                                            error_message=error, error_type="NonZeroCode")

    def non_zero_error_version(self, error, module, prev_info, details):
        versions, prev_str = self._fetch_versions_and_history(module, prev_info, details)
        parser = JsonOutputParser(pydantic_object=ModuleVersion)

        if self.rag:
            tpl = (
                "Given a comma separated list of 'Module versions' for the '{module}' module, "
                "from oldest to newest ({module_versions})\nPerform equally distanced sampling "
                "to return a version from the given versions, excluding previously used versions "
                "({previous_versions}). Return the information with the format {format_instructions}"
            )
            pvars = {"error": error, "module": module, "python_version": details["python_version"],
                      "module_versions": versions, "previous_versions": prev_str,
                      "format_instructions": parser.get_format_instructions()}
        else:
            tpl = (
                "Infer a possible working version of the '{module}' module for Python "
                "{python_version}.\nReturn the information with the format {format_instructions}"
            )
            pvars = {"error": error, "module": module, "python_version": details["python_version"],
                      "format_instructions": parser.get_format_instructions()}

        ver_prompt = PromptTemplate(template=tpl, input_variables=[], partial_variables=pvars)
        out = self._get_version_excluding_previous(ver_prompt, parser, prev_str,
                                                    module=module, details=details)
        return out if out and "module" in out and "version" in out else None

    # ── Central error dispatcher ─────────────────────────────────────
    def process_error(self, message, error_details, llm_eval):
        """Classify a Docker build/run error and call the appropriate handler.
        Returns (output_dict, error_type_string)."""
        error_type = "None"
        output = None

        if "Could not find a version" in message:
            if self.logging: print("Could not find a version")
            error_type = "VersionNotFound"
            output = self.could_not_find_version(message, error_details, llm_eval)
        elif "dependency conflicts" in message:
            if self.logging: print("Dependency conflict")
            error_type = "DependencyConflict"
            output = self.dependency_conflict(message)
        elif "ImportError" in message:
            if self.logging: print("Import Error")
            error_type = "ImportError"
            if "DJANGO_SETTINGS_MODULE is undefined" in message:
                output = None
            else:
                output = self.import_error(message, error_details, llm_eval)
        elif "ModuleNotFoundError" in message:
            if self.logging: print("Module not found")
            error_type = "ModuleNotFound"
            output = self.module_not_found(message, error_details, llm_eval)
        elif "AttributeError" in message:
            if self.logging: print("Attribute error")
            error_type = "AttributeError"
            output = self.attribute_error(message, error_details, llm_eval)
        elif "InvalidVersion" in message:
            if self.logging: print("Invalid Version")
            error_type = "InvalidVersion"
            output = self.invalid_version(message)
        elif "non-zero code" in message:
            if self.logging: print("Non-zero error code from docker build")
            error_type = "NonZeroCode"
            output = self.non_zero_error(message)
            output = self.non_zero_error_version(message, output, error_details, llm_eval)
        elif "SyntaxError" in message:
            if self.logging: print("Syntax Error")
            error_type = "SyntaxError"
            output = self.syntax_error_helper(message, error_details, llm_eval)
        else:
            if self.logging: print("No error type found")

        return output, error_type

