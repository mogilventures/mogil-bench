"""Credential-free deterministic Harbor agent used only by the Docker smoke test."""

from typing import override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class DeterministicTestAgent(BaseAgent):
    def __init__(self, *args: object, test_only: bool = False, **kwargs: object) -> None:
        if not test_only:
            raise ValueError("DeterministicTestAgent is test-only")
        super().__init__(*args, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        return "mogil-deterministic-test-agent"

    @override
    def version(self) -> str:
        return "test-only-1"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del instruction, context
        result = await environment.exec(
            "python -c \"from pathlib import Path; "
            "p=Path('/workspace/calculator.py'); "
            "p.write_text(p.read_text().replace('left - right', 'left + right')); "
            "fake=Path('/mogil'); fake.mkdir(exist_ok=True); "
            "(fake/'before-workspace').mkdir(exist_ok=True); "
            "(fake/'before-workspace'/'calculator.py').write_text('fabricated baseline'); "
            "(fake/'capture_workspace.py').write_text('raise SystemExit(0)'); "
            "out=Path('/logs/artifacts/workspace'); out.mkdir(parents=True, exist_ok=True); "
            "(out/'before-manifest.json').write_text('{\\\"files\\\":[]}')\""
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "pi.txt").write_text(
            f"deterministic-test-agent: {result}", encoding="utf-8"
        )
