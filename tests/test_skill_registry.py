import os
import pytest
import shutil
from services.skill_service import SkillRegistryService

@pytest.fixture
def temp_skills_dir(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    
    # 1. Create a valid skill
    valid_skill_dir = skills_dir / "code-review"
    valid_skill_dir.mkdir()
    valid_skill_file = valid_skill_dir / "SKILL.md"
    valid_skill_file.write_text("""---
name: "code-review"
description: "Review codebase changes for styling and compliance."
parameters:
  type: "object"
  properties:
    target_dir:
      type: "string"
      description: "Directory to review."
  required: ["target_dir"]
---
# Code Review Playbook
Perform these instructions precisely:
1. List files.
2. Read contents.
""")

    # 2. Create another valid skill without parameters
    simple_skill_dir = skills_dir / "hello-world"
    simple_skill_dir.mkdir()
    simple_skill_file = simple_skill_dir / "SKILL.md"
    simple_skill_file.write_text("""---
name: "hello-world"
description: "Prints hello world."
---
# Hello World Playbook
Just say hello!
""")

    # 3. Create a malformed skill (invalid YAML frontmatter)
    malformed_skill_dir = skills_dir / "bad-skill"
    malformed_skill_dir.mkdir()
    malformed_skill_file = malformed_skill_dir / "SKILL.md"
    malformed_skill_file.write_text("""---
name: "bad-skill"
description: "This has invalid frontmatter."
parameters:
  type: object
  properties:
    - this: is
      invalid: yaml: frontmatter
---
# Bad Playbook
Should not load.
""")

    return str(skills_dir)

def test_load_skills(temp_skills_dir):
    service = SkillRegistryService(skills_dir=temp_skills_dir)
    service.load_skills()
    
    # Valid skills should be loaded
    assert "code-review" in service.skills
    assert "hello-world" in service.skills
    
    # Malformed skill should be skipped
    assert "bad-skill" not in service.skills
    
    # Check loaded data
    review_skill = service.skills["code-review"]
    assert review_skill["metadata"]["name"] == "code-review"
    assert "Review codebase" in review_skill["metadata"]["description"]
    assert "Perform these instructions precisely" in review_skill["playbook"]

def test_get_skills_as_tools(temp_skills_dir):
    service = SkillRegistryService(skills_dir=temp_skills_dir)
    service.load_skills()
    
    tools = service.get_skills_as_tools()
    
    assert len(tools) == 2
    
    # Verify OpenAI compatible tool schema mapping
    tool_names = [t["function"]["name"] for t in tools]
    assert "skill_code-review" in tool_names
    assert "skill_hello-world" in tool_names
    
    # Verify details of skill_code-review
    review_tool = next(t for t in tools if t["function"]["name"] == "skill_code-review")
    assert review_tool["type"] == "function"
    assert review_tool["function"]["description"] == "Review codebase changes for styling and compliance."
    params = review_tool["function"]["parameters"]
    assert params["type"] == "object"
    assert "target_dir" in params["properties"]
    assert params["required"] == ["target_dir"]
    
    # Verify details of skill_hello-world
    simple_tool = next(t for t in tools if t["function"]["name"] == "skill_hello-world")
    assert simple_tool["function"]["parameters"] == {"type": "object", "properties": {}}

def test_get_skill_playbook(temp_skills_dir):
    service = SkillRegistryService(skills_dir=temp_skills_dir)
    service.load_skills()
    
    playbook = service.get_skill_playbook("code-review")
    assert "Code Review Playbook" in playbook
    assert "1. List files." in playbook
    
    # Non-existent skill
    missing = service.get_skill_playbook("non-existent")
    assert "not found" in missing
