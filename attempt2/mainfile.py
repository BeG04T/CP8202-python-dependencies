import os
import re
import sys
import time
import tarfile
import shutil
import tempfile
import subprocess
import importlib.util
import sysconfig
import yaml

from datetime import datetime
from typing import Dict, List

TAR_FILE        = "hard-gists.tar.gz"   
RESULTS_DIR     = "results"            
MODULES_CACHE   = "modules_cache"       
SNIPPETS_DIR    = "snippets"           
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL    = "gemma2"
OLLAMA_TEMP     = 0.7
MAX_LLM_RETRIES = 5
MAX_SNIPPETS    = 20    #Note for eric: Initially testing with 20 snippets. If we set the value to None, it will process all snippets.


def import_dependencies():
    """
    Import third-party deps here so we can give a clear error message
    if the user forgot to pip install requirements.txt.
    """
    try:
        from pypi_json import PyPIJSON
        from langchain_community.chat_models import ChatOllama
        from langchain_core.prompts import PromptTemplate
        from langchain_core.output_parsers import JsonOutputParser
        from langchain_core.pydantic_v1 import BaseModel, Field
        return PyPIJSON, ChatOllama, PromptTemplate, JsonOutputParser, BaseModel, Field
    except ImportError as e:
        print(f"\nERROR: Missing dependency — {e}")
        print("Run:  pip install -r requirements.txt\n")
        sys.exit(1)

#Extracting the tar file. Note for eric: The prompt inside is AI Generated. I have added comments to make it more clear and easier to understand.
def extract_tar(tar_path: str) -> List[dict]:
    """
    Extracts hard-gists.tar.gz.

    Real structure (confirmed):
        hard-gists/<gist_id>/snippet.py

    Skips:
        - hard-gists/._<anything>   (macOS AppleDouble metadata)
        - hard-gists/.DS_Store

    Returns list of dicts:
        { gist_id, local_path }
    """#
    print(f"\n{'='*60}")
    print(f"STEP 1: Extracting {tar_path}")
    print(f"{'='*60}")

    os.makedirs(SNIPPETS_DIR, exist_ok=True)
    os.makedirs(MODULES_CACHE, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    snippets    = []
    skipped     = 0
    already_had = 0

    with tarfile.open(tar_path, 'r:gz') as tar:
        members = tar.getmembers()
        print(f"  Archive members total : {len(members)}")

        for member in members:
            if not member.isfile():
                continue

            parts    = member.name.split('/') 
            basename = os.path.basename(member.name)

            if basename.startswith('._') or basename == '.DS_Store':
                skipped += 1
                continue

            if len(parts) != 3:
                skipped += 1
                continue

            gist_id  = parts[1]
            filename = parts[2]

            if not filename.endswith('.py'):
                skipped += 1
                continue

            safe_name  = f"{gist_id}__snippet.py"
            local_path = os.path.join(SNIPPETS_DIR, safe_name)

            if os.path.exists(local_path):
                already_had += 1
            else:
                try:
                    f       = tar.extractfile(member)
                    content = f.read()
                    with open(local_path, 'wb') as out:
                        out.write(content)
                except Exception as e:
                    print(f"  [WARN] Could not extract {member.name}: {e}")
                    continue

            snippets.append({
                'gist_id':    gist_id,
                'local_path': local_path,
                'safe_name':  safe_name,
            })

    print(f"  Skipped (junk/non-py) : {skipped}")
    print(f"  Already extracted     : {already_had}")
    print(f"  Usable snippets       : {len(snippets)}")
    return snippets

#LIb filter
_STDLIB_EXTRAS = {
    'os', 'sys', 'io', 're', 'json', 'time', 'math', 'datetime',
    'collections', 'itertools', 'functools', 'pathlib', 'typing',
    'abc', 'copy', 'enum', 'logging', 'threading', 'multiprocessing',
    'subprocess', 'hashlib', 'hmac', 'random', 'string', 'struct',
    'socket', 'ssl', 'http', 'urllib', 'email', 'html', 'xml', 'csv',
    'sqlite3', 'unittest', 'argparse', 'shutil', 'tempfile', 'traceback',
    'warnings', 'weakref', 'gc', 'inspect', 'ast', 'dis', 'token',
    'tokenize', 'importlib', 'contextlib', 'dataclasses', 'queue',
    'heapq', 'bisect', 'array', 'codecs', 'base64', 'binascii',
    'textwrap', 'pprint', 'decimal', 'fractions', 'statistics',
    'platform', 'signal', 'glob', 'fnmatch', 'linecache', 'pickle',
    'shelve', 'marshal', 'zipfile', 'gzip', 'bz2', 'lzma', 'tarfile',
    'getpass', 'getopt', 'cmd', 'code', 'codeop', 'compileall',
    'py_compile', 'dis', 'symtable', 'pyclbr', 'formatter',
    'types', 'builtins', '__future__', 'numbers', 'cmath',
    'operator', 'reprlib', 'concurrent', 'asyncio', 'selectors',
}

def is_stdlib(module: str) -> bool:
    if not module:
        return True
    m = module.strip().lower()
    if m in sys.builtin_module_names:
        return True
    if m in _STDLIB_EXTRAS:
        return True
    try:
        spec = importlib.util.find_spec(m)
        if spec is None or spec.origin is None:
            return False
        return spec.origin.startswith(sysconfig.get_paths()['stdlib'])
    except Exception:
        return False


#import scraper. Note for eric: The prompt inside is AI Generated. I have added comments to make it more clear and easier to understand.

def scrape_imports(filepath: str) -> List[str]:
    """
    Regex-based import scraper.  Handles:
        import X
        import X as Y
        from X import Y
        from X.Y.Z import W
    Skips commented lines and block-quoted sections.
    Returns only non-stdlib module names.
    """
    imports      = []
    in_docstring = False

    try:
        with open(filepath, 'r', errors='ignore') as f:
            for line in f:
                s = line.strip()

                # Toggle block-quote tracking
                triple = s.count('"""') + s.count("'''")
                if triple % 2 == 1:             # odd count = toggle
                    in_docstring = not in_docstring
                if in_docstring:
                    continue
                if s.startswith('#') or not s:
                    continue

                # import X  /  import X as Y  /  import X, Y
                m = re.match(r'^import\s+([\w\s,]+)', s)
                if m:
                    for part in m.group(1).split(','):
                        mod = part.strip().split()[0].split('.')[0]
                        if mod and mod not in imports:
                            imports.append(mod)

                # from X import ...
                m = re.match(r'^from\s+([\w\.]+)\s+import', s)
                if m:
                    mod = m.group(1).split('.')[0]
                    if mod and mod not in imports:
                        imports.append(mod)

    except Exception as e:
        print(f"    [scrape] {filepath}: {e}")

    return [
        x for x in imports
        if x
        and not x.startswith('_')
        and not is_stdlib(x)
    ]




def llm_evaluate(model, filepath: str, JsonOutputParser, PromptTemplate,
                 BaseModel, Field) -> dict:
    """
    Sends snippet to gemma2 and asks for:
        - python_version  (e.g. "3.9")
        - python_modules  (list of third-party pip packages)

    Retries with exponential backoff. Falls back to 3.8 / empty list.
    """


    class PythonFile(BaseModel):
        python_version: str = Field(description="Python version e.g. 3.9")
        python_modules: List[str] = Field(
            description="Third-party pip-installable module names only"
        )

    try:
        with open(filepath, 'r', errors='ignore') as f:
            raw = f.read()
    except Exception:
        return {'python_version': '3.8', 'python_modules': []}

    # Truncate very large files to stay within context window
    if len(raw) > 6000:
        raw = raw[:6000] + "\n# ... (truncated for analysis)"

    parser = JsonOutputParser(pydantic_object=PythonFile)

    prompt = PromptTemplate(
        template=(
            "Analyze this Python file and identify:\n"
            "1. The minimum Python version needed to run it (e.g. 2.7, 3.6, 3.9)\n"
            "2. All third-party pip-installable packages it imports\n\n"
            "Do NOT include standard library modules (os, sys, re, json, etc).\n\n"
            "Python file:\n{raw_file}\n\n"
            "Return ONLY valid JSON matching this schema:\n{format_instructions}"
        ),
        input_variables=[],
        partial_variables={
            "raw_file": raw,
            "format_instructions": parser.get_format_instructions(),
        }
    )

    chain = prompt | model | parser

    for attempt in range(MAX_LLM_RETRIES):
        try:
            out = chain.invoke({})

            # Normalise fields
            out['python_version'] = str(out.get('python_version', '3.8'))
            mods = out.get('python_modules', [])
            if isinstance(mods, dict):
                mods = list(mods.keys())
            out['python_modules'] = [str(m).strip() for m in mods if m]
            return out

        except Exception as e:
            wait = 2 ** attempt
            print(f"    [LLM attempt {attempt+1}/{MAX_LLM_RETRIES}] {e} "
                  f"— retrying in {wait}s")
            time.sleep(wait)

    print("    [LLM] All retries exhausted — using fallback values")
    return {'python_version': '3.8', 'python_modules': []}


# Python version lifecycle dates used to pick compatible package versions
_PYTHON_DATES = {
    '2.7':  ('2010-07-03', '2020-01-01'),
    '3.5':  ('2015-09-13', '2020-09-13'),
    '3.6':  ('2016-12-23', '2021-12-23'),
    '3.7':  ('2018-06-27', '2023-06-27'),
    '3.8':  ('2019-10-14', '2024-10-14'),
    '3.9':  ('2020-10-05', '2025-10-05'),
    '3.10': ('2021-10-04', '2026-10-04'),
    '3.11': ('2022-10-24', '2027-10-24'),
    '3.12': ('2023-10-02', '2028-10-02'),
}

def normalize_pyver(v: str) -> str:
    """'3.9.1' → '3.9',  '3' → '3.8',  'python3.10' → '3.10'"""
    digits = re.sub(r'[^0-9\.]', '', v).strip('.')
    parts  = digits.split('.')
    if not parts or not parts[0]:
        return '3.8'
    major = parts[0]
    minor = parts[1] if len(parts) > 1 and parts[1] not in ('', 'x') else '8'
    return f"{major}.{minor}"


def _ver_key(v: str):
    return [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', v)]


def fetch_pypi_versions(module: str, python_version: str,
                        PyPIJSON) -> List[str]:
    """
    Returns a sorted list of PyPI versions for `module` that are compatible
    with `python_version`.

    Cache: modules_cache/<module>_<pyver>.txt
    On cache hit → instant return, no network call.
    """
    norm       = normalize_pyver(python_version)
    cache_path = os.path.join(MODULES_CACHE, f"{module}_{norm}.txt")

    # ── cache hit ────────────────────────────────────────────────────────────
    if os.path.isfile(cache_path):
        with open(cache_path) as f:
            cached = f.read().strip()
        if cached:
            return [v.strip() for v in cached.split(',') if v.strip()]

    # ── PyPI query ───────────────────────────────────────────────────────────
    start_str, end_str = _PYTHON_DATES.get(norm, ('2019-10-14', '2024-10-14'))
    fmt        = '%Y-%m-%d'
    start_date = datetime.strptime(start_str, fmt).date()
    end_date   = datetime.strptime(end_str,   fmt).date()

    versions = []
    try:
        with PyPIJSON() as client:
            meta = client.get_metadata(module)

        for ver_str, releases in meta.releases.items():
            for rel in releases:
                if rel.get('yanked'):
                    continue
                raw_time = rel.get('upload_time', '')
                if not raw_time:
                    continue
                try:
                    upload_date = datetime.strptime(
                        raw_time.split('T')[0], '%Y-%m-%d'
                    ).date()
                except ValueError:
                    continue

                py_tag = rel.get('python_version', '')

                in_window = start_date <= upload_date <= end_date
                tag_match = (
                    norm.replace('.', '') in py_tag          # cp39, cp310 …
                    or ('py3' in py_tag and norm.startswith('3'))
                    or ('py2' in py_tag and norm.startswith('2'))
                    or py_tag in ('source', 'any', '')
                )

                if in_window or tag_match:
                    if ver_str not in versions:
                        versions.append(ver_str)
                break   # one release file per version is enough

        versions = sorted(versions, key=_ver_key)

        # Fallback: nothing matched → take last 20 overall releases
        if not versions:
            all_v = sorted(meta.releases.keys(), key=_ver_key)
            versions = all_v[-20:]

    except Exception as e:
        print(f"    [PyPI] {module}: {e}")

    # ── write cache ──────────────────────────────────────────────────────────
    if versions:
        with open(cache_path, 'w') as f:
            f.write(', '.join(versions))

    return versions


def resolve_modules(modules: List[str], python_version: str,
                    PyPIJSON) -> Dict[str, str]:
    """
    For every module name → fetch PyPI versions → pick most recent.
    Returns { module: version }.
    """
    resolved = {}
    for raw_mod in modules:
        mod = raw_mod.strip().lower()
        if not mod or is_stdlib(mod) or mod.startswith('_'):
            continue

        versions = fetch_pypi_versions(mod, python_version, PyPIJSON)
        if versions:
            chosen          = versions[-1]   # most recent compatible version
            resolved[mod]   = chosen
            print(f"    [resolve] {mod:30s} → {chosen}")
        else:
            print(f"    [resolve] {mod:30s} → NOT FOUND on PyPI")

    return resolved

def validate_with_venv(resolved: Dict[str, str]) -> tuple:
    """
    Creates a throwaway virtual environment and attempts to pip install
    every resolved package. Cleans up after itself.

    Returns:
        (passed: bool, output: str)

    Why this replaces Docker:
      - Proves the resolved versions are co-installable (the hard problem)
      - No container runtime needed
      - Same pip resolver that real projects use
      - Result is reproducible and logged verbatim in the YAML
    """
    if not resolved:
        return True, "No external modules — nothing to validate."

    tmpdir   = tempfile.mkdtemp(prefix="pllm_venv_")
    venv_dir = os.path.join(tmpdir, 'venv')
    lines    = []

    try:
        # Create venv
        r = subprocess.run(
            [sys.executable, '-m', 'venv', venv_dir],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return False, f"venv creation failed:\n{r.stderr.strip()}"

        # Locate pip (Linux/macOS vs Windows)
        pip = os.path.join(venv_dir, 'bin', 'pip')
        if not os.path.isfile(pip):
            pip = os.path.join(venv_dir, 'Scripts', 'pip.exe')

        # Upgrade pip silently so version-resolution is up to date
        subprocess.run(
            [pip, 'install', '--upgrade', 'pip', '-q'],
            capture_output=True
        )

        all_ok = True
        for module, version in resolved.items():
            pkg = f"{module}=={version}" if version else module
            r   = subprocess.run(
                [pip, 'install', pkg, '--timeout=60', '-q'],
                capture_output=True, text=True
            )
            ok   = (r.returncode == 0)
            icon = '✓' if ok else '✗'
            line = f"pip install {pkg}: {icon}"
            lines.append(line)
            print(f"      {line}")

            if not ok:
                all_ok = False
                err = (r.stderr or r.stdout).strip()
                if err:
                    lines.append(f"  → {err[:400]}")

        return all_ok, '\n'.join(lines)

    except Exception as e:
        return False, f"venv exception: {e}"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def write_yaml(snippet: dict, python_version: str,
               resolved: Dict[str, str], passed: bool,
               venv_output: str, llm_raw: dict,
               static_imports: List[str],
               start_time: float) -> str:
    """
    Writes one YAML file per snippet under results/.
    Filename: results/<gist_id>__snippet.yml
    """
    elapsed  = time.time() - start_time
    out_name = snippet['safe_name'].replace('.py', '.yml')
    out_path = os.path.join(RESULTS_DIR, out_name)

    data = {
        # ── identity ──────────────────────────────────────────────────────
        'gist_id':              snippet['gist_id'],
        'original_file':        'snippet.py',
        # ── timing ────────────────────────────────────────────────────────
        'start_time':           round(start_time, 3),
        'end_time':             round(start_time + elapsed, 3),
        'total_time_seconds':   round(elapsed, 3),
        # ── environment ───────────────────────────────────────────────────
        'python_version':       python_version,
        'validation_method':    'venv_pip_install',
        'validation_passed':    bool(passed),
        # ── analysis ──────────────────────────────────────────────────────
        'llm_output': {
            'python_version': llm_raw.get('python_version', 'unknown'),
            'python_modules': llm_raw.get('python_modules', []),
        },
        'static_imports':       static_imports,
        'resolved_modules':     resolved,
        # ── validation detail ─────────────────────────────────────────────
        'validation_output':    venv_output,
    }

    with open(out_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)

    return out_path


def process_snippet(snippet: dict, model,
                    PyPIJSON, JsonOutputParser,
                    PromptTemplate, BaseModel, Field) -> dict:
    """
    Runs the full pipeline for one snippet:
        static scrape → LLM → merge → PyPI resolve → venv → YAML
    """
    start = time.time()
    print(f"\n  Gist : {snippet['gist_id']}")

    # 1. Static scrape
    print("  [1] Static import scrape")
    static = scrape_imports(snippet['local_path'])
    print(f"      {static or '(none)'}")

    # 2. LLM
    print("  [2] LLM (gemma2)")
    llm_raw = llm_evaluate(
        model, snippet['local_path'],
        JsonOutputParser, PromptTemplate, BaseModel, Field
    )
    print(f"      version  : {llm_raw['python_version']}")
    print(f"      modules  : {llm_raw['python_modules']}")

    # 3. Merge static + LLM, deduplicate
    pyver    = normalize_pyver(llm_raw.get('python_version', '3.8'))
    llm_mods = llm_raw.get('python_modules', [])
    if isinstance(llm_mods, dict):
        llm_mods = list(llm_mods.keys())

    combined = list(dict.fromkeys(
        m.strip().lower()
        for m in (static + llm_mods)
        if m and not is_stdlib(m) and not m.strip().startswith('_')
    ))
    print(f"  [3] Combined ({len(combined)}): {combined}")

    # 4. PyPI resolve
    print("  [4] PyPI resolution")
    resolved = resolve_modules(combined, pyver, PyPIJSON)

    # 5. Venv validate
    print("  [5] Venv validation")
    passed, venv_out = validate_with_venv(resolved)
    elapsed = time.time() - start
    print(f"  [6] {'PASS ✓' if passed else 'FAIL ✗'}  ({elapsed:.1f}s)")

    # 6. Write YAML
    yaml_path = write_yaml(
        snippet=snippet, python_version=pyver,
        resolved=resolved, passed=passed,
        venv_output=venv_out, llm_raw=llm_raw,
        static_imports=static, start_time=start,
    )
    print(f"  [7] YAML → {yaml_path}")

    return {
        'gist_id':          snippet['gist_id'],
        'python_version':   pyver,
        'modules_found':    len(combined),
        'modules_resolved': len(resolved),
        'passed':           passed,
        'elapsed_seconds':  round(elapsed, 2),
        'yaml':             yaml_path,
    }

def write_summary(results: list):
    real    = [r for r in results if not r.get('skipped')]
    passed  = sum(1 for r in real if r.get('passed'))
    failed  = len(real) - passed
    rate    = f"{passed / len(real) * 100:.1f}%" if real else "N/A"

    summary = {
        'total_snippets': len(results),
        'processed':      len(real),
        'passed':         passed,
        'failed':         failed,
        'pass_rate':      rate,
        'results':        results,
    }

    path = os.path.join(RESULTS_DIR, '_summary.yml')
    with open(path, 'w') as f:
        yaml.dump(summary, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)

    print(f"\n{'='*60}")
    print("  FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"  Snippets processed : {len(real)}")
    print(f"  Passed             : {passed}")
    print(f"  Failed             : {failed}")
    print(f"  Pass rate          : {rate}")
    print(f"  Summary written    : {path}")

def main():
    print("\n" + "="*60)
    print("  PLLM Test Pipeline  (no Docker)")
    print(f"  Model   : {OLLAMA_MODEL}  @ {OLLAMA_BASE_URL}")
    print(f"  Dataset : {TAR_FILE}")
    print(f"  Limit   : {MAX_SNIPPETS or 'ALL'} snippets")
    print("="*60)

    if not os.path.isfile(TAR_FILE):
        print(f"\nERROR: '{TAR_FILE}' not found in current directory.")
        print("Make sure you run this script from the same folder as the tar file.")
        sys.exit(1)

    (PyPIJSON, ChatOllama, PromptTemplate,
     JsonOutputParser, BaseModel, Field) = import_dependencies()

    snippets = extract_tar(TAR_FILE)
    if not snippets:
        print("ERROR: No Python snippets found in archive.")
        sys.exit(1)

    if MAX_SNIPPETS:
        print(f"\n[INFO for eric] Limiting to first {MAX_SNIPPETS} snippets for this run.")
        snippets = snippets[:MAX_SNIPPETS]

    # ── init Ollama once (reused for every snippet) ───────────────────────────
    print(f"\nConnecting to Ollama at {OLLAMA_BASE_URL} ...")
    try:
        model = ChatOllama(
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            format="json",
            temperature=OLLAMA_TEMP,
        )
        print("  Connected ✓\n")
    except Exception as e:
        print(f"\nERROR: Could not connect to Ollama — {e}")
        print("Make sure Ollama is running:  ollama serve")
        sys.exit(1)

    # ── main loop ─────────────────────────────────────────────────────────────
    results = []
    total   = len(snippets)

    for idx, snippet in enumerate(snippets, 1):
        print(f"\n{'─'*60}")
        print(f"[{idx}/{total}]")

        # Skip already-processed snippets (safe to re-run)
        expected_yaml = os.path.join(
            RESULTS_DIR,
            snippet['safe_name'].replace('.py', '.yml')
        )
        if os.path.exists(expected_yaml):
            print(f"  Already processed — skipping {snippet['gist_id']}")
            results.append({'gist_id': snippet['gist_id'], 'skipped': True})
            continue

        try:
            result = process_snippet(
                snippet, model,
                PyPIJSON, JsonOutputParser,
                PromptTemplate, BaseModel, Field
            )
            results.append(result)
        except Exception as e:
            print(f"\n  [ERROR] {snippet['gist_id']}: {e}")
            results.append({
                'gist_id': snippet['gist_id'],
                'passed':  False,
                'error':   str(e),
            })

    write_summary(results)
    print("\nDone.\n")


if __name__ == "__main__":
    main()