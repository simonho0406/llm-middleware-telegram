import os

def check_file(path):
    with open(path, 'r') as f:
        lines = f.readlines()
    
    issues = []
    for i, line in enumerate(lines):
        if 'except Exception' in line:
            context = "".join(lines[i:i+4])
            issues.append(f"--- {path}:{i+1} ---\n{context}")
    return issues

if __name__ == '__main__':
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    all_issues = []
    for root, _, files in os.walk(project_dir):
        if 'venv' in root or '.git' in root or '__pycache__' in root or 'tests' in root:
            continue
        for file in files:
            if file.endswith('.py'):
                all_issues.extend(check_file(os.path.join(root, file)))

    print(f"Total Exception blocks found: {len(all_issues)}\n")
    for issue in all_issues[:30]:
        print(issue)
