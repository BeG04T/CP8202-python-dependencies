# First step is to effectively attempt to recreate the PLLM pipeline, and if not 
# Step 1: extract all module names
# Step 2: Query Python version
# Step 3: Limit Module Versions 
# Step 4: Query Module versions
# Step 5: Attempt build project
# Step 6: Loop if failure.
# Almsot all code below is either directly copied from the pllm project or modified on top of it. 
# This line will be changed when major modifications are made

# Handle argument parsing
import argparse
import json
import time
import multiprocessing as mp
import os

import subprocess

from multiprocessing import Process

from helpers.ollama_helper_tester import OllamaHelper
from helpers.py_pi_query import PyPIQuery
from helpers.build_dockerfile import DockerHelper
from helpers.deps_scraper import DepsScraper

class TestExecutor():

    def __init__(self, base_url="http://localhost:11434", model='gemma2', logging=True, temp=0.7, end_loop=5, search_range=1, base_modules='./modules') -> None:
        # Initiate instance of Ollama helper and PyPi Query
        print(f'Running model- {model} with temp {temp}. Looping {end_loop} times with a search range of {search_range}')
        self.ollama_helper = OllamaHelper(base_url=base_url, model=model, logging=logging, temp=temp, base_modules=base_modules)
        self.pypi = PyPIQuery(logging=True, base_modules=base_modules)
        self.deps = DepsScraper(logging=True)
        self.end_loop = end_loop
        self.search_range = search_range
        self.start_time = time.time()
        pass

    # Defines JSONObject dictionary for dot notation
    def validate_json(self, json_string):
        try:
            json.loads(json_string)
        except ValueError as err:
            return False
        return True

    # Reads the contents of the given file
    def read_python_file(self, file):
        with open(file, 'r') as file:
            data = file.read().replace('\n', '')
        return data

    def evaluate_file(self, llm, file):
        # First LLM pass- Evaluates the Python file and gives us the initial JSON
        llm_eval = llm.evaluate_file(file)
        llm_eval['python_version'] = str(llm_eval['python_version'])

        # Should normally be a list. Re-format to a list if it is a dict.
        python_modules = llm_eval['python_modules']
        if type(python_modules) == dict:
            list_modules = []
            for module in python_modules:
                list_modules.append(module)
            llm_eval['python_modules'] = list_modules

        return llm_eval

    def get_module_specifics(self, llm, llm_eval):
        # Uses the modules from the LLM output to get a specific set of versions for the inferred Python version
        # Also returns an updated python version, based on what the model had provided
        llm_eval['python_modules'], llm_eval['python_version'] = llm.pypi.get_module_specifics(llm_eval)
        
        module_versions = llm.get_module_versions(llm_eval)
        llm_eval['python_modules'] = module_versions

        return llm_eval

    def build_container(self, dockerHelper, llm, llm_eval, file, error_details = {}):
        # Build the docker image with the given JSON and file/ paths
        dockerHelper.create_dockerfile(llm_eval, file)
        passed, docker_build_output = dockerHelper.build_dockerfile(file)
        if not passed:
            print(docker_build_output)
            output, error_type = llm.process_error(docker_build_output, error_details, llm_eval)
            print(f"docker build failed!")
            return False, docker_build_output, output, error_type
        else:
            print(f"docker build complete!")
            return True, docker_build_output, None, None

    # Handle and update modules and versions that have previously had errors
    # Updates the 'error_modules' list to feed back ot the model later
    def naughty_bois(self, module, error_handler, error_type, llm_eval):
        error_handler[error_type] += 1
        error_handler['previous'] = error_type

        if module != None and module['module'] in llm_eval['python_modules']:
            if module['module'] in error_handler['error_modules']:
                error_handler['error_modules'][module['module']].append(llm_eval['python_modules'][module['module']])
            else:
                error_handler['error_modules'][module['module']] = [llm_eval['python_modules'][module['module']]]
        else:
            print('No previous this time!')

        return error_handler


    # Update the llm details
    # Set previous modules, so our output is correct
    # Removes and adds modules based on the new module returned by the LLM
    def update_llm_eval(self, new, llm_eval):
        details = llm_eval.copy()
        details['previous_python_modules'] = details['python_modules'].copy()
        if new != None:
            module_name = self.pypi.check_module_name(new['module'])
            module_name = module_name[0] if len(module_name) > 0 else module_name
            # Check to see if we need to pop a module or add the new version
            if new['version'] == None or new['version'] == 'None' or new['version'] == 'none' or new['version'] == '' and module_name in details['python_modules']:
                details['python_modules'].pop(module_name)
            else:
                details['python_modules'][module_name] = new['version']
        return details
        
    # Append module to the given list
    def append_module(self, module_name, list):
        return module_name in list
    
    # Method to shuffle the dependencies
    # This is to ensure dependencies are installed in the correct order
    def shuffle_modules(self, new_module, move_module, llm_details):
        modules = []
        python_modules = llm_details['python_modules'].copy()
        for module in python_modules:
            if module == move_module:
                if not self.append_module(new_module, modules): modules.append(new_module)
                if not self.append_module(move_module, modules): modules.append(move_module)
            else:
                if not self.append_module(module, modules): modules.append(module)

        llm_details['python_modules'] = {module: python_modules[module] for module in modules}
        return llm_details

    # Main docker process loop
    # This method is given as a process to run in parallel with each other
    # Handles the main loop of building | running | validating
    def docker_create_process(self, ollama_helper, llm_eval, file, process_num, return_dict, outpFile):

        #Edit Attempt?

        #pip install all modules
        #run code
        #pip uninstall all modules

        # Below code is copied over from Build_Docker Helper

        # The code assumes i have a venv running.

        #4 stands for timeout
        

        print("Reached Docker_Create")

        llm_eval = self.get_module_specifics(ollama_helper, llm_eval)

        python_modules = llm_eval['python_modules']
        print(python_modules)
        return_dict[process_num] = 4

        line = ["pip","install","--trusted-host","pypi.python.org","--default-timeout=100"]
        for module in python_modules:
            if type(module) == dict:
                name = module['module']
                version = module['version']
            else:
                name = module
                version = python_modules[module]

            # if self.logging: print(type(data))
            # if self.logging: print(data)
            
            if type(version) == str:
                print(f"""ADDING "{name}=={version}"\n""")
                line.append(f"{name}=={version}")
            else:
                print(f"""ADDING "{name}=={version[0]}"\n""")
                line.append(f"{name}=={version[0]}")

        print("Final Line for Running")
        print(line)
        outpFile.write(' '.join(line))
        outpFile.write("\n")
        pip_process = subprocess.run(line,capture_output=True, text=True)
        status = (pip_process.returncode == 0)

        if status:
            print(f"{name}=={version} install successful")
            
        else:
            print(f"{name}=={version} install error, error reason: \n" + pip_process.stdout)
            return_dict[process_num] = 1
            return 1
    
        # While the following code is included, running is not necessary as we lack the ability to run old/decrepit python versions. !This downside will be included in the report!
        run_process = subprocess.run(["python3", file],
        capture_output=True, text=True)
        status = (run_process.returncode == 0)
        if status:
            print(file + " Code ran success!")
        else:
            print(file + "ran unsuccesful, error reason: \n" + run_process.stdout)

        #Cleanup, removes all pip installs
        for module in python_modules:
            if type(module) == dict:
                name = module['module']
                version = module['version']
            else:
                name = module
                version = python_modules[module]

            line = []
            if type(version) == str:
                print(f"""RUN ["pip","uninstall","--trusted-host","pypi.python.org","--default-timeout=100","{name}=={version}"]\n""")
                line = ["pip","uninstall","--trusted-host","pypi.python.org","--default-timeout=100",f"{name}=={version}"]
            else:
                print(f"""RUN ["pip","uninstall","--trusted-host","pypi.python.org","--default-timeout=100","{name}=={version[0]}"]\n""")
                line = ["pip","uninstall","--trusted-host","pypi.python.org","--default-timeout=100",f"{name}=={version[0]}"]
            
            pip_process = subprocess.run(line,capture_output=True, text=True)
            status = (pip_process.returncode == 0)

            if not status:
                print(f"{name}=={version} uninstall error, error reason: \n" + pip_process.stdout)
                return_dict[process_num] = 3
                return 3

        return_dict[process_num] = 0
        return 0

    # Logging specific, ensures correct spaces in log file to avoid later errors
    def ensure_8_spaces(self, line):
        if not line.startswith(' ' * 8):
            return ' ' * 8 + line.lstrip()
        return line

    # Fixes lines in error message as the outputs from the docker logs can be wildy different
    def fix_error_line(self, line):
        if '\t' in line:
            line = line.replace('\t', '  ')

        if 'TabError:' in line:
            line = '  ' + line

        # Remove any special characters
        if '␛' in line or '␈' in line or '␛' in line or '␛[' in line:
            line = line.replace('␛', '').replace('␈', '').replace('␛', '').replace('␛[', '')

        # Ensure a line is indented correctly
        if 'ETA' in line or '0us/step' in line:
            line = self.ensure_8_spaces(line)

        return line

    # Handles the logging of the error messages and iterations to the log file
    def end_test(self, file_to_open, llm_eval, dockerHelper, error_type, docker_message, loop, run_complete):
        out_file = open(file_to_open, "a")
        python_modules = llm_eval["previous_python_modules"] if 'previous_python_modules' in llm_eval else llm_eval['python_modules']
        out_file.write(f"  iteration_{loop}:\n")
        out_file.write(f'    - python_module: {python_modules}\n')
        out_file.write(f'    - error_type: {error_type}\n')
        out_file.write(f'    - error: |\n')
        if '"stream"' in docker_message:
            error_message = docker_message.replace('{"stream":"', '').replace(':', '')
            docker_message = error_message[:-5]
        previous_line = ''
        extend = ''
        for line in docker_message.split('\n'):
            if not line == '':
            # if not line == '' and not 'errorDetail' in line:
                if '^' in previous_line: extend = '  '  #and not 'iteration' in previous_line else '' # If there's a '^' in the previous line then we need to indent more for formatting
                out_line = f'        {line}\n'
                out_line = self.fix_error_line(out_line)
                out_file.write(f'{extend}{out_line}')
                previous_line = line
        print(loop)
        if loop + 1 > self.end_loop or run_complete:
            end_time = time.time()
            out_file.write(f'end_time: {end_time}\n')
            out_file.write(f'total_time: {end_time - self.start_time}')
            out_file.close()
            dockerHelper.delete_container()
            dockerHelper.delete_image()
            exit(0)
        else:
            return loop + 1




def process_args():
    def str2bool(value):
        """Convert a string representation of a boolean to an actual boolean value."""
        if isinstance(value, bool):
            return value
        if value.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif value.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError(f'Boolean value expected, got "{value}".')

    parser = argparse.ArgumentParser(description='File to evaluate')
    parser.add_argument('-f', '--file', type=str, help="The full path and name of the file to evaluate")
    parser.add_argument('-b', '--base', type=str, nargs="?", default='http://localhost:11434', const='http://localhost:11434', help="The ollama URL can vary depending on the system")
    parser.add_argument('-m', '--model', type=str, nargs="?", default='phi3:medium', const='phi3:medium', help="The name of the model to use for evaluation")
    parser.add_argument('-t', '--temp', type=str, nargs="?", default='0.7', const='0.7', help="The temperature for the models predictive output. Typically a range from 0-2, default is 0.7")
    parser.add_argument('-l', '--loop', type=int, nargs="?", default=5, const=5, help="How many times we will loop to find a solution")
    parser.add_argument('-r', '--range', type=int, nargs="?", default=0, const=0, help="The search range, expands out above and below the found Python version, defaults to 0")
    parser.add_argument('-ra', '--rag', type=str2bool, nargs="?", default=True, const=True, help="Flag to enable RAG in the system.")
    parser.add_argument('-v', '--verbose', action="store_true", help="Verbose logging of information")
    return parser.parse_args()


#modified attempt
def main():
    output_file = open("Eric_pllm_results.txt", "w")
    # Process the arguments, file, model ...
    args = process_args()
    # the line below is probably irrelevant now but i'm going to keep it in case it breaks lol
    # the main change is the filepath is now the filepath to the DIRECTORY that includes all the snippets
    file_path = '/'.join(args.file.split('/')[:])

    filepaths = []
    for i_file in os.scandir(args.file):
        if i_file.path[-7:] != "modules" and not ("._" in i_file.path):
            filepaths.append(i_file.path)

    print(filepaths[0:3])

    total = 0
    total_pass = 0
    #ATM THIS IS ONLY FOR TESTING, WE WILL PROBABLY NOT RUN EVERYTHING BUT FUTURE CHANGE IS EITHER HAVE THIS LOOP THROUGH ALL FILES, OR SCRAMBLE THE FILE LISTS THEN CHOOSE A SMALL SUBSET
    for j in range(0, 3):
        llm_eval = None
        llm_details = False
        loop = 0
        
        filepath = filepaths[j] + "/snippet.py"
    
        # Create the main 
        testExecutor = TestExecutor(base_url=args.base, model=args.model, logging=True, temp=args.temp, end_loop=args.loop, search_range=args.range, base_modules=file_path+"/modules")
        # Use a simple search to grab imports from file without the LLM
        python_deps = []
        if args.rag:
            python_deps = testExecutor.deps.find_word_in_file(filepath, 'import', [])
    
        # Loop to ensure we handle invalid responses from the model
        while not llm_details:
            try:
                # Evaluate the file to get an initial set of assumptions
                llm_eval = testExecutor.evaluate_file(testExecutor.ollama_helper, filepath)
                
                # Run through all the dependencies and clean them for use. Removes useless imports
                python_deps = testExecutor.pypi.check_module_name(python_deps + llm_eval['python_modules'])
    
                # Combine the simple search modules with the LLMs suggestions.
                llm_eval['python_modules'] = python_deps
    
                print(llm_eval)
                llm_details = True
            except Exception as e:
                print(f"Failed to get Python modules from file: {e}")
                llm_details = False
                loop += 1
            
            if loop >= 5: break
        # If the LLM didn't return anything, set the Python version to 3.8
        if not llm_details:
            llm_eval = {'python_version': '3.8'}
            llm_eval['python_modules'] = testExecutor.pypi.check_module_name(python_deps)
    
        # testExecutor.docker_create_process(ollama_helper, llm_eval, filepath, 1)
        # Search range is how far either side of the found Python verion we want to look.
        # For example, a value of 1 where the found version is 3.7 will return [3.6,3.7,3.8]
        python_versions = testExecutor.pypi.get_python_range(python_version=llm_eval['python_version'], pyrange=testExecutor.search_range)
        print(python_versions)
        
        # If python_versions is empty then there was an issue with versions.
        # Give the lowest Python and work with this range
        if not python_versions:
            python_versions = testExecutor.pypi.get_python_range(python_version=llm_eval['python_version'], range=testExecutor.search_range)
        num_processes = (testExecutor.search_range * 2) + 1
    
        processes = []
    
        # To access values from processes
        manager = mp.Manager()
        return_dict = manager.dict()
        
        # NOTE: CHANGE THIS TO TEST SPECIFIC VERSION
        # python_versions = ['3.8']
    
        # Create and start the processes
        for i in range(num_processes):
            run_details = llm_eval.copy()
            # Select a version from the python range
            run_details['python_version'] = python_versions[i]
            # run_details['python_version'] = '3.6'
            # Give the docker create process, ollama helper, the snippet analysis, python file and the iteration
            p = mp.Process(
                target=testExecutor.docker_create_process,
                args=(
                    OllamaHelper(base_url=args.base, model=args.model, logging=True, temp=args.temp, base_modules=file_path+"/modules", rag=args.rag),
                    run_details,
                    filepath,
                    i,
                    return_dict,
                    output_file)
                )
            processes.append(p)
            p.start()
    
        # Wait for all processes to finish
        for p in processes:
            # Give the process 20 minutes to complete
            p.join(timeout=600)
        
        for p in processes:
            if p.is_alive():
                p.terminate()
            else:
                print("Processing completed without the timeout")
    
        output_file.write(i_file.path.split("/")[-1])
        output_file.write("\n Status: ")
        print(return_dict.values())
        if 0 in return_dict.values():
            print("Dependency resolved at least once!")
            output_file.write("0 \n")
            total_pass += 1
        else:
            error_val = max(set(return_dict.values()), key=return_dict.values().count)
            output_file.write(f"{error_val} \n")

        total += 1

    print(f"Out of {total} gists, {total_pass} successfully passed")
    output_file.write(f"Out of {total} gists, {total_pass} successfully passed")

if __name__ == "__main__":
    main()

    print(f"Done")
