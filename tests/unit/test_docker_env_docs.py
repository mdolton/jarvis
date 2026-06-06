from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_ENV_VARS = {
    "JARVIS_MEMORY_EMBEDDING_MODEL",
    "JARVIS_MEMORY_EMBEDDING_DIMENSIONS",
}


def _compose_environment(path: str) -> set[str]:
    data = yaml.safe_load((REPO_ROOT / path).read_text())
    return set(data["services"]["jarvis"]["environment"])


def test_compose_files_forward_memory_environment_variables():
    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        assert MEMORY_ENV_VARS <= _compose_environment(compose_file)


def test_env_example_documents_memory_environment_variables():
    env_example = (REPO_ROOT / ".env.example").read_text()

    for var in MEMORY_ENV_VARS:
        assert var in env_example
