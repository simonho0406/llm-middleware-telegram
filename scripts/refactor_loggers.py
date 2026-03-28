import os
import re

def process_file(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()

    new_lines = []
    in_except_block = False
    exception_var = None

    for i, line in enumerate(lines):
        # Match 'except Exception as e:' or 'except Exception:'
        except_match = re.search(r'^\s*except\s+Exception(?:\s+as\s+(\w+))?\s*:', line)
        
        if except_match:
            in_except_block = True
            exception_var = except_match.group(1)
            new_lines.append(line)
            continue
        
        if in_except_block:
            # Check if this line within the block is a generic logger error/warning
            logger_match = re.search(r'^(\s*)logger\.(error|warning)\((.*)\)', line)
            
            # If we exit the block's indentation level early or find a structural break, turn off state
            if line.strip() and not line.startswith(' '*4) and not line.startswith('\t'):
                in_except_block = False
            
            if logger_match:
                indent = logger_match.group(1)
                log_type = logger_match.group(2)
                args = logger_match.group(3)
                
                # Check if it was manually forcing exc_info=True already
                if 'exc_info=' in args or 'traceback' in args:
                     new_lines.append(line)
                     in_except_block = False
                     continue
                
                # Clean up the {e} string interpolation
                # "f'failed: {e}'" -> "f'failed'" if possible, but safely just replacing logger.XXX with logger.exception
                # Let's just blindly upcast to logger.exception to dump the stacktrace, retaining original message.
                new_line = line.replace(f"logger.{log_type}(", "logger.exception(")
                new_lines.append(new_line)
                
                # We only want to target the very first logger found right after the except block
                in_except_block = False
                continue
            
        new_lines.append(line)

    if new_lines != lines:
        with open(filepath, 'w') as f:
            f.writelines(new_lines)
        print(f"Refactored exception catch blocks in: {filepath}")

def run():
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for root, _, files in os.walk(project_dir):
        if 'venv' in root or '.git' in root or '__pycache__' in root or 'tests' in root:
            continue
        for file in files:
            if file.endswith('.py'):
                process_file(os.path.join(root, file))

if __name__ == '__main__':
    run()
